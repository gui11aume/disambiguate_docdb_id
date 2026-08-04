"""Tests for the hosted citation-resolving web app."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from docdb_id.web import db
from docdb_id.web import server as web_server
from docdb_id.web.mcp_agent import _tool_result_payload, run_agent

# ── db.py unit tests ─────────────────────────────────────────────────────────


@pytest.fixture()
def conn(tmp_path: Path):
    c = db.connect(str(tmp_path / "web.sqlite3"))
    yield c
    c.close()


def test_get_or_create_user_returns_same_id_for_same_email(conn):
    first = db.get_or_create_user(conn, "a@example.com")
    second = db.get_or_create_user(conn, "a@example.com")
    assert first == second

    other = db.get_or_create_user(conn, "b@example.com")
    assert other != first


def test_create_and_consume_magic_link_returns_email(conn):
    token = db.create_magic_link(conn, "a@example.com", ttl_minutes=15)
    email = db.consume_magic_link(conn, token)
    assert email == "a@example.com"


def test_consume_magic_link_rejects_expired_token(conn):
    token = db.create_magic_link(conn, "a@example.com", ttl_minutes=-1)
    assert db.consume_magic_link(conn, token) is None


def test_consume_magic_link_rejects_reused_token(conn):
    token = db.create_magic_link(conn, "a@example.com", ttl_minutes=15)
    assert db.consume_magic_link(conn, token) == "a@example.com"
    assert db.consume_magic_link(conn, token) is None


def test_count_requests_last_24h_excludes_older_requests(conn):
    user_id = db.get_or_create_user(conn, "a@example.com")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO requests (user_id, created_at) VALUES (?, ?)",
        (user_id, old_time),
    )
    conn.commit()
    db.log_request(conn, user_id)

    assert db.count_requests_last_24h(conn, user_id) == 1


# ── mcp_agent.py unit tests (mock ClientSession) ─────────────────────────────


def test_run_agent_returns_content_when_no_tool_calls():
    import asyncio

    session = SimpleNamespace(
        list_tools=_async_return(SimpleNamespace(tools=[])),
    )
    message = SimpleNamespace(tool_calls=None, content="Hello world")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))
    )

    result = asyncio.run(run_agent(session, openai_client, "model", "hi"))
    assert result == "Hello world"


def test_run_agent_dispatches_tool_call_and_returns_final_content():
    import asyncio

    tool = SimpleNamespace(name="resolve_docdb_id", description="desc", inputSchema={})
    call_result = SimpleNamespace(
        isError=False,
        structuredContent={"result": [{"docdb_id": "US8000000B2"}]},
        content=[],
    )

    calls = []

    async def fake_call_tool(name, args):
        calls.append((name, args))
        return call_result

    session = SimpleNamespace(
        list_tools=_async_return(SimpleNamespace(tools=[tool])),
        call_tool=fake_call_tool,
    )

    tool_call = SimpleNamespace(
        id="call1",
        function=SimpleNamespace(
            name="resolve_docdb_id",
            arguments=json.dumps({"items": [{"cc": "US", "number": "8000000"}]}),
        ),
    )
    first_msg = SimpleNamespace(tool_calls=[tool_call], content=None)
    second_msg = SimpleNamespace(tool_calls=None, content="US8000000B2")
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=first_msg)]),
        SimpleNamespace(choices=[SimpleNamespace(message=second_msg)]),
    ]

    def fake_create(**kwargs):
        return responses.pop(0)

    openai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))

    result = asyncio.run(run_agent(session, openai_client, "model", "US 8,000,000"))

    assert result == "US8000000B2"
    assert calls == [("resolve_docdb_id", {"items": [{"cc": "US", "number": "8000000"}]})]


def test_tool_result_payload_raises_on_error():
    result = SimpleNamespace(isError=True, content=[SimpleNamespace(text="boom")])
    with pytest.raises(RuntimeError, match="boom"):
        _tool_result_payload(result)


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


# ── Integration tests (FastAPI TestClient) ──────────────────────────────────


@pytest.fixture()
def sent_emails(monkeypatch):
    sent = []

    def fake_send(email: str, link_url: str) -> None:
        sent.append((email, link_url))

    monkeypatch.setattr(web_server, "send_magic_link_email", fake_send)
    return sent


@pytest.fixture()
def resolve_calls(monkeypatch):
    calls = []

    async def fake_resolve_text(text: str) -> str:
        calls.append(text)
        return f"RESOLVED:{text}"

    monkeypatch.setattr(web_server, "resolve_text", fake_resolve_text)
    return calls


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_emails, resolve_calls):
    monkeypatch.setattr(web_server, "WEB_DB_PATH", str(tmp_path / "web.sqlite3"))
    # base_url must be https: the session cookie is Secure (server.py), and
    # Starlette's default http://testserver base_url would silently drop it
    # from the client's cookie jar, breaking every test that logs in once
    # and expects the session to carry over to a later request.
    with TestClient(web_server.app, base_url="https://testserver") as c:
        yield c


def _extract_token(db_path: str, email: str) -> str:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT token FROM magic_links WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email,),
        ).fetchone()
        return row["token"]
    finally:
        conn.close()


def _login(client: TestClient, tmp_path: Path, email: str) -> int:
    """Create a user and session directly via db helpers, then set the cookie."""
    conn = db.connect(str(tmp_path / "web.sqlite3"))
    try:
        user_id = db.get_or_create_user(conn, email)
        token = db.create_session(conn, user_id, ttl_days=30)
    finally:
        conn.close()
    client.cookies.set("session", token)
    return user_id


def test_request_link_returns_200_and_sends_email(client: TestClient, sent_emails):
    resp = client.post("/auth/request-link", json={"email": "a@example.com"})
    assert resp.status_code == 200
    assert len(sent_emails) == 1
    email, link_url = sent_emails[0]
    assert email == "a@example.com"
    assert "/auth/verify?token=" in link_url


def test_verify_creates_session_and_redirects(client: TestClient, tmp_path: Path):
    client.post("/auth/request-link", json={"email": "a@example.com"})
    token = _extract_token(str(tmp_path / "web.sqlite3"), "a@example.com")

    resp = client.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    assert "session" in resp.cookies


def test_verify_rejects_invalid_token(client: TestClient):
    resp = client.get("/auth/verify?token=bogus", follow_redirects=False)
    assert resp.status_code == 401


def test_me_unauthenticated_returns_401(client: TestClient):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_me_returns_200_after_verify(client: TestClient, tmp_path: Path):
    """Regression test: the session cookie is HttpOnly, so the frontend can't
    detect login state by reading document.cookie - it must ask /auth/me."""
    client.post("/auth/request-link", json={"email": "a@example.com"})
    token = _extract_token(str(tmp_path / "web.sqlite3"), "a@example.com")
    client.get(f"/auth/verify?token={token}", follow_redirects=False)

    resp = client.get("/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True}


def test_full_auth_flow_then_resolve(client: TestClient, tmp_path: Path, resolve_calls):
    client.post("/auth/request-link", json={"email": "a@example.com"})
    token = _extract_token(str(tmp_path / "web.sqlite3"), "a@example.com")
    client.get(f"/auth/verify?token={token}", follow_redirects=False)

    resp = client.post("/resolve", json={"text": "US 8,000,000 (Greenberg)"})
    assert resp.status_code == 200
    assert resp.json() == {"text": "RESOLVED:US 8,000,000 (Greenberg)"}
    assert resolve_calls == ["US 8,000,000 (Greenberg)"]


def test_resolve_unauthenticated_returns_401(client: TestClient):
    resp = client.post("/resolve", json={"text": "US 8,000,000 (Greenberg)"})
    assert resp.status_code == 401


def test_resolve_input_too_long_returns_422_without_llm_call(
    client: TestClient, tmp_path: Path, resolve_calls
):
    _login(client, tmp_path, "a@example.com")
    too_long = "x" * (web_server.WEB_MAX_INPUT_CHARS + 1)

    resp = client.post("/resolve", json={"text": too_long})
    assert resp.status_code == 422
    assert resolve_calls == []


def test_resolve_quota_exceeded_returns_429(client: TestClient, tmp_path: Path):
    user_id = _login(client, tmp_path, "a@example.com")

    conn = db.connect(str(tmp_path / "web.sqlite3"))
    try:
        for _ in range(web_server.WEB_DAILY_QUOTA):
            db.log_request(conn, user_id)
    finally:
        conn.close()

    resp = client.post("/resolve", json={"text": "US 8,000,000 (Greenberg)"})
    assert resp.status_code == 429
