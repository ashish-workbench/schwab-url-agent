import hashlib
import sqlite3
import string
import time
from pathlib import Path
from urllib.parse import urlparse

ALPHABET = string.digits + string.ascii_letters
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "links.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            code TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            clicks INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            expires_at REAL
        )
        """
    )
    return conn


def _code_for(url: str, n: int = 7) -> str:
    digest = hashlib.sha256(f"{url}:{time.time_ns()}".encode()).digest()
    num = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(n):
        chars.append(ALPHABET[num % 62])
        num //= 62
    return "".join(chars)


def validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only http(s) URLs with a host are allowed")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URL are rejected")
    return url.strip()


def create_link(conn: sqlite3.Connection, url: str, ttl_seconds: int | None = None) -> dict:
    clean = validate_url(url)
    expires = time.time() + ttl_seconds if ttl_seconds else None
    for _ in range(5):
        code = _code_for(clean)
        try:
            conn.execute(
                "INSERT INTO links (code, url, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (code, clean, time.time(), expires),
            )
            conn.commit()
            return get_link(conn, code)
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate code")


def get_link(conn: sqlite3.Connection, code: str) -> dict | None:
    row = conn.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
    return dict(row) if row else None


def is_expired(link: dict, now: float | None = None) -> bool:
    exp = link.get("expires_at")
    if exp is None:
        return False
    return (now if now is not None else time.time()) >= exp


def record_click(conn: sqlite3.Connection, code: str) -> dict | None:
    link = get_link(conn, code)
    if not link or is_expired(link):
        return None
    conn.execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
    conn.commit()
    return get_link(conn, code)
