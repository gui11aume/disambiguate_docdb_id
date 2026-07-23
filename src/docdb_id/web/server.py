"""FastAPI server for the hosted citation-cleaning web app.

Endpoints: GET / (static UI), POST /auth/request-link, GET /auth/verify,
POST /clean. Sits behind nginx at docdb.sarl-graip.fr's `/`, `/auth/`, and
`/clean` routes (see deploy/nginx.conf), fully additive to the existing
/query, /batch, /health, /stats, /mcp surface.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, field_validator

from docdb_id.web import db
from docdb_id.web.mcp_agent import clean_text

logger = logging.getLogger("docdb_id.web")

STATIC_DIR = Path(__file__).parent / "static"

WEB_DB_PATH = os.environ.get("WEB_DB_PATH", "web.sqlite3")
WEB_DAILY_QUOTA = int(os.environ.get("WEB_DAILY_QUOTA", "20"))
WEB_MAGIC_LINK_TTL_MINUTES = int(os.environ.get("WEB_MAGIC_LINK_TTL_MINUTES", "15"))
WEB_SESSION_TTL_DAYS = int(os.environ.get("WEB_SESSION_TTL_DAYS", "30"))
WEB_MAX_INPUT_CHARS = int(os.environ.get("WEB_MAX_INPUT_CHARS", "4000"))

SCALEWAY_TEM_API_KEY = os.environ.get("SCALEWAY_TEM_API_KEY", "")
SCALEWAY_TEM_REGION = os.environ.get("SCALEWAY_TEM_REGION", "fr-par")
SCALEWAY_TEM_PROJECT_ID = os.environ.get("SCALEWAY_TEM_PROJECT_ID", "")
SCALEWAY_TEM_SENDER = os.environ.get("SCALEWAY_TEM_SENDER", "noreply@docdb.sarl-graip.fr")

SESSION_COOKIE_NAME = "session"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(WEB_DB_PATH)
    conn.close()
    yield


app = FastAPI(title="DOCDB Citation Cleaner", lifespan=lifespan)


class RequestLinkBody(BaseModel):
    """Body for POST /auth/request-link."""
    email: str


class CleanBody(BaseModel):
    """Body for POST /clean."""
    text: str

    @field_validator("text")
    @classmethod
    def check_length(cls, v: str) -> str:
        if len(v) > WEB_MAX_INPUT_CHARS:
            raise ValueError(f"text exceeds max length of {WEB_MAX_INPUT_CHARS} characters")
        return v


def send_magic_link_email(email: str, link_url: str) -> None:
    """Send a magic-link sign-in email via Scaleway's Transactional Email API.

    Args:
        email: Recipient address.
        link_url: Fully-qualified verification URL to embed in the email.

    Raises:
        httpx.HTTPStatusError: If the Scaleway TEM API rejects the request.
    """
    resp = httpx.post(
        f"https://api.scaleway.com/transactional-email/v1alpha1/regions/{SCALEWAY_TEM_REGION}/emails",
        headers={"X-Auth-Token": SCALEWAY_TEM_API_KEY},
        json={
            "project_id": SCALEWAY_TEM_PROJECT_ID,
            "from": {"email": SCALEWAY_TEM_SENDER},
            "to": [{"email": email}],
            "subject": "Your DOCDB sign-in link",
            "text": f"Sign in to DOCDB citation cleaner:\n\n{link_url}\n\n"
                    f"This link expires in {WEB_MAGIC_LINK_TTL_MINUTES} minutes.",
        },
        timeout=10.0,
    )
    resp.raise_for_status()


async def get_current_user_id(session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME)) -> int:
    """FastAPI dependency resolving the session cookie to a user id.

    Args:
        session: Session cookie value, injected by FastAPI.

    Returns:
        The authenticated user's id.

    Raises:
        HTTPException 401: If there is no session cookie or it is invalid/expired.
    """
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")

    def _sync() -> int | None:
        conn = db.connect(WEB_DB_PATH)
        try:
            return db.get_session_user(conn, session)
        finally:
            conn.close()

    user_id = await anyio.to_thread.run_sync(_sync)
    if user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user_id


@app.get("/")
async def index() -> FileResponse:
    """Serve the single-page citation-cleaning UI.

    Returns:
        The static index.html file.
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/auth/request-link")
async def request_link(body: RequestLinkBody, request: Request) -> dict:
    """Issue a magic sign-in link and email it to the given address.

    Always returns 200 regardless of whether the email is already registered,
    so this endpoint cannot be used to enumerate accounts.

    Args:
        body: Request body containing the target email address.
        request: Incoming request, used to build the absolute verify URL.

    Returns:
        {"status": "ok"}
    """
    def _sync() -> str:
        conn = db.connect(WEB_DB_PATH)
        try:
            return db.create_magic_link(conn, body.email, WEB_MAGIC_LINK_TTL_MINUTES)
        finally:
            conn.close()

    token = await anyio.to_thread.run_sync(_sync)
    link_url = str(request.base_url.replace(path="/auth/verify")) + f"?token={token}"

    try:
        await anyio.to_thread.run_sync(send_magic_link_email, body.email, link_url)
    except Exception:
        logger.exception("failed to send magic-link email to %s", body.email)

    return {"status": "ok"}


@app.get("/auth/verify")
async def verify(token: str) -> Response:
    """Validate and consume a magic-link token, then establish a session.

    Args:
        token: The magic-link token from the emailed URL.

    Returns:
        A redirect to `/` with the session cookie set.

    Raises:
        HTTPException 401: If the token is missing, expired, or already used.
    """
    def _sync() -> str:
        conn = db.connect(WEB_DB_PATH)
        try:
            email = db.consume_magic_link(conn, token)
            if email is None:
                return None
            user_id = db.get_or_create_user(conn, email)
            return db.create_session(conn, user_id, WEB_SESSION_TTL_DAYS)
        finally:
            conn.close()

    session_token = await anyio.to_thread.run_sync(_sync)
    if session_token is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=WEB_SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.post("/clean")
async def clean(body: CleanBody, user_id: int = Depends(get_current_user_id)) -> dict:
    """Replace patent citations in `body.text` with canonical DOCDB IDs.

    Requires a valid session cookie. Enforces the rolling 24h per-account
    quota before making any LLM call.

    Args:
        body: Request body containing the text to clean (already
            length-checked by `CleanBody.check_length`).
        user_id: The authenticated user, injected by `get_current_user_id`.

    Returns:
        {"text": str} with citations replaced by canonical DOCDB IDs.

    Raises:
        HTTPException 429: If the account has hit `WEB_DAILY_QUOTA` requests
            in the last rolling 24h.
    """
    def _check_quota() -> int:
        conn = db.connect(WEB_DB_PATH)
        try:
            return db.count_requests_last_24h(conn, user_id)
        finally:
            conn.close()

    request_count = await anyio.to_thread.run_sync(_check_quota)
    if request_count >= WEB_DAILY_QUOTA:
        raise HTTPException(status_code=429, detail="daily quota exceeded")

    result_text = await clean_text(body.text)

    def _log() -> None:
        conn = db.connect(WEB_DB_PATH)
        try:
            db.log_request(conn, user_id)
        finally:
            conn.close()

    await anyio.to_thread.run_sync(_log)

    return {"text": result_text}


def main() -> None:
    """Run the FastAPI server with uvicorn."""
    import uvicorn
    uvicorn.run("docdb_id.web.server:app", host="0.0.0.0", port=8002)
