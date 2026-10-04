from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.store import connect, create_link, get_link, is_expired, record_click

app = FastAPI(title="url-shortener-prototype")
DB = Path(__file__).resolve().parent.parent / "data" / "links.db"


class CreateIn(BaseModel):
    url: str
    ttl_seconds: int | None = None


def _db():
    return connect(DB)


@app.post("/links")
def post_link(body: CreateIn):
    try:
        link = create_link(_db(), body.url, body.ttl_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": link["code"], "short_url": f"/{link['code']}", "expires_at": link["expires_at"]}


@app.get("/links/{code}/stats")
def stats(code: str):
    link = get_link(_db(), code)
    if not link:
        raise HTTPException(404, "missing")
    return {
        "code": link["code"],
        "clicks": link["clicks"],
        "created_at": link["created_at"],
        "expires_at": link["expires_at"],
        "expired": is_expired(link),
    }


@app.get("/{code}")
def redirect(code: str):
    link = record_click(_db(), code)
    if not link:
        raise HTTPException(404, "missing or expired")
    return RedirectResponse(link["url"], status_code=302)
