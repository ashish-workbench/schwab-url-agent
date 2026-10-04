"""Persistence and domain rules for the URL shortener."""

from __future__ import annotations

import secrets
import sqlite3
import string
import time
from pathlib import Path
from urllib.parse import urlparse

ALPHABET = string.digits + string.ascii_letters
CODE_LENGTH = 7
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_TTL_SECONDS = 31536000
DEFAULT_TTL_SECONDS = 2592000

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    clicks INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL
)
"""


class InvalidLink(ValueError):
    """Raised when a submitted URL or TTL violates link policy."""


def _now() -> float:
    return time.time()


def connect(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, backward-compatible migration for databases created before expiry existed."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(links)")}
    if "expires_at" not in columns:
        with conn:
            conn.execute("ALTER TABLE links ADD COLUMN expires_at REAL")


def new_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def validate_url(url: str) -> str:
    candidate = url.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        raise InvalidLink(f"url must be 1..{MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        raise InvalidLink("url must not contain whitespace or control characters")
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
        raise InvalidLink("only http(s) URLs with a host are allowed")
    if parsed.username or parsed.password:
        raise InvalidLink("credentials in URLs are rejected")
    return candidate


def resolve_ttl(ttl_seconds: int | None) -> int | None:
    if ttl_seconds is None:
        return DEFAULT_TTL_SECONDS
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise InvalidLink(f"ttl_seconds must be 1..{MAX_TTL_SECONDS}")
    return ttl_seconds


def create_link(
    conn: sqlite3.Connection, url: str, ttl_seconds: int | None = None, now: float | None = None
) -> dict:
    clean = validate_url(url)
    created = _now() if now is None else now
    ttl = resolve_ttl(ttl_seconds)
    expires = created + ttl if ttl is not None else None
    for _ in range(5):
        code = new_code()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO links (code, url, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (code, clean, created, expires),
                )
        except sqlite3.IntegrityError:
            continue
        return get_link(conn, code)
    raise RuntimeError("could not allocate a unique code after 5 attempts")


def get_link(conn: sqlite3.Connection, code: str) -> dict | None:
    row = conn.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
    return dict(row) if row else None


def is_expired(link: dict, now: float | None = None) -> bool:
    expires = link.get("expires_at")
    if expires is None:
        return False
    return (_now() if now is None else now) >= expires


def record_click(conn: sqlite3.Connection, code: str, now: float | None = None) -> dict | None:
    """Atomically count a click. Returns None when the code is unknown or expired."""
    current = _now() if now is None else now
    with conn:
        cur = conn.execute(
            "UPDATE links SET clicks = clicks + 1 "
            "WHERE code = ? AND (expires_at IS NULL OR expires_at > ?)",
            (code, current),
        )
    if cur.rowcount == 0:
        return None
    return get_link(conn, code)
