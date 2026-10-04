import sqlite3

import pytest

from app import store


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "links.db")
    yield c
    c.close()


def test_create_and_click(conn):
    link = store.create_link(conn, "https://example.com/a")
    assert len(link["code"]) == store.CODE_LENGTH
    assert store.record_click(conn, link["code"])["clicks"] == 1
    assert store.record_click(conn, link["code"])["clicks"] == 2


def test_unknown_code_returns_none(conn):
    assert store.record_click(conn, "nope123") is None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "ftp://example.com",
        "https://",
        "notaurl",
        "https://exa mple.com",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(store.InvalidLink):
        store.validate_url(url)


def test_rejects_credentials_in_url():
    with pytest.raises(store.InvalidLink):
        store.validate_url("https://user:pass@example.com")


def test_rejects_overlong_url():
    with pytest.raises(store.InvalidLink):
        store.validate_url("https://example.com/" + "a" * store.MAX_URL_LENGTH)


def test_codes_are_unique(conn):
    codes = {store.create_link(conn, "https://example.com")["code"] for _ in range(200)}
    assert len(codes) == 200


def test_expired_link_is_not_followed(conn):
    link = store.create_link(conn, "https://example.com", ttl_seconds=10, now=1000.0)
    assert store.record_click(conn, link["code"], now=1005.0) is not None
    assert store.record_click(conn, link["code"], now=1010.0) is None
    assert store.is_expired(link, now=1010.0)


@pytest.mark.parametrize("ttl", [0, -1, store.MAX_TTL_SECONDS + 1])
def test_rejects_out_of_range_ttl(conn, ttl):
    with pytest.raises(store.InvalidLink):
        store.create_link(conn, "https://example.com", ttl_seconds=ttl)


def test_migration_keeps_existing_links(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE links (code TEXT PRIMARY KEY, url TEXT NOT NULL, "
        "clicks INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
    )
    legacy.execute("INSERT INTO links VALUES ('legacy1', 'https://example.com', 3, 1.0)")
    legacy.commit()
    legacy.close()
    c = store.connect(path)
    link = store.get_link(c, "legacy1")
    assert link["clicks"] == 3
    assert link["expires_at"] is None
    assert store.record_click(c, "legacy1")["clicks"] == 4
    c.close()


def test_default_ttl_applied(conn):
    link = store.create_link(conn, "https://example.com", now=0.0)
    assert link["expires_at"] == store.DEFAULT_TTL_SECONDS
