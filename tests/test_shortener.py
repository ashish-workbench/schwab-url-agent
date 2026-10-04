import time

from app.store import connect, create_link, is_expired, record_click, validate_url
import pytest


def test_create_and_click(tmp_path):
    conn = connect(tmp_path / "t.db")
    link = create_link(conn, "https://example.com/a")
    assert link["code"]
    clicked = record_click(conn, link["code"])
    assert clicked["clicks"] == 1


def test_rejects_bad_scheme():
    with pytest.raises(ValueError):
        validate_url("javascript:alert(1)")


def test_expiry_blocks_redirect(tmp_path):
    conn = connect(tmp_path / "t.db")
    link = create_link(conn, "https://example.com/a", ttl_seconds=-1)
    assert is_expired(link, now=time.time() + 5)
    assert record_click(conn, link["code"]) is None
