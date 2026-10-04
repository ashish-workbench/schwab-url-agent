"""HTTP API for the URL shortener."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app import store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "links.db"
CODE_PATTERN = r"^[0-9A-Za-z]{4,32}$"

app = FastAPI(title="URL Shortener", version="1.0.0")


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = store.connect(os.environ.get("SHORTENER_DB", DEFAULT_DB))
    try:
        yield conn
    finally:
        conn.close()


class CreateLinkIn(BaseModel):
    url: str = Field(..., min_length=1, max_length=store.MAX_URL_LENGTH)
    ttl_seconds: int | None = Field(default=None, gt=0, le=store.MAX_TTL_SECONDS)


class LinkOut(BaseModel):
    code: str
    short_url: str
    url: str
    expires_at: float | None


class StatsOut(BaseModel):
    code: str
    clicks: int
    created_at: float
    expires_at: float | None
    expired: bool


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/links", status_code=201, response_model=LinkOut)
def create(body: CreateLinkIn, conn: sqlite3.Connection = Depends(get_conn)) -> LinkOut:
    try:
        link = store.create_link(conn, body.url, body.ttl_seconds)
    except store.InvalidLink as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LinkOut(
        code=link["code"],
        short_url=f"/{link['code']}",
        url=link["url"],
        expires_at=link["expires_at"],
    )


@app.get("/links/{code}/stats", response_model=StatsOut)
def stats(
    code: str = PathParam(..., pattern=CODE_PATTERN),
    conn: sqlite3.Connection = Depends(get_conn),
) -> StatsOut:
    link = store.get_link(conn, code)
    if link is None:
        raise HTTPException(status_code=404, detail="link not found")
    return StatsOut(
        code=link["code"],
        clicks=link["clicks"],
        created_at=link["created_at"],
        expires_at=link["expires_at"],
        expired=store.is_expired(link),
    )


@app.get("/{code}")
def follow(
    code: str = PathParam(..., pattern=CODE_PATTERN),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    link = store.record_click(conn, code)
    if link is None:
        if store.get_link(conn, code) is not None:
            raise HTTPException(status_code=410, detail="link expired")
        raise HTTPException(status_code=404, detail="link not found")
    return RedirectResponse(link["url"], status_code=302)
