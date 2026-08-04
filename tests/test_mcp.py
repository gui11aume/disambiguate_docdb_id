"""Unit tests for the MCP resolve_docdb_id tool and prompts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from docdb_id.agent.default_prompt import (
    DEFAULT_SYSTEM_PROMPT,
    NORMALIZE_CITATIONS_WORKFLOW,
    ROLE,
)
from docdb_id.mcp.server import MAX_MCP_BATCH, ItemToResolve, mcp, resolve_docdb_id

_FAKE_BATCH = [
    {
        "cc": "US",
        "number": "1",
        "results": [
            {
                "docdb_id": "US1A",
                "inventor": "TEST",
                "date_publ": "20000101",
                "family_id": "1",
            }
        ],
        "error": None,
    },
    {
        "cc": "EP",
        "number": "2",
        "results": [],
        "error": None,
    },
]


@pytest.fixture()
def mcp_session(monkeypatch: pytest.MonkeyPatch):
    """Local in-memory MCP client session backed by the DOCDB FastMCP server."""
    monkeypatch.setenv("DOCDB_API_URL", "http://docdb.test")

    @asynccontextmanager
    async def _session():
        with patch("docdb_id.mcp.server.httpx.post") as post:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = _FAKE_BATCH
            post.return_value = resp
            async with create_connected_server_and_client_session(mcp) as session:
                yield session

    return _session


def _payload_from_tool_result(result: Any) -> list[dict]:
    structured = result.structuredContent
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    if isinstance(structured, list):
        return structured
    raise AssertionError("MCP tool returned no structured list payload")


def test_resolve_docdb_id_rejects_empty_items():
    with pytest.raises(ValueError, match="non-empty"):
        resolve_docdb_id([])


def test_resolve_docdb_id_rejects_over_cap():
    items = [ItemToResolve(cc="US", number=str(i)) for i in range(MAX_MCP_BATCH + 1)]
    with patch("docdb_id.mcp.server.httpx.post") as post:
        with pytest.raises(ValueError, match=f"max {MAX_MCP_BATCH}"):
            resolve_docdb_id(items)
    post.assert_not_called()


def test_resolve_docdb_id_mcp_interface(mcp_session):
    """Connect to a local MCP server and check the resolve_docdb_id interface."""

    async def _run() -> list[dict]:
        async with mcp_session() as session:
            tools = await session.list_tools()
            assert any(t.name == "resolve_docdb_id" for t in tools.tools)

            result = await session.call_tool(
                "resolve_docdb_id",
                {
                    "items": [
                        {"cc": "US", "number": "1"},
                        {"cc": "EP", "number": "2"},
                    ],
                },
            )
            assert not result.isError
            return _payload_from_tool_result(result)

    payload = asyncio.run(_run())
    assert isinstance(payload, list)
    assert len(payload) == 2
    for item in payload:
        assert isinstance(item, dict)
        assert "cc" in item
        assert "number" in item
        assert "results" in item
        assert "error" in item
        assert isinstance(item["results"], list)
        for record in item["results"]:
            assert set(record) >= {"docdb_id", "inventor", "date_publ", "family_id"}


def test_prompt_sections_compose():
    """Agent prompt = role + workflow; workflow has no persona framing."""
    assert DEFAULT_SYSTEM_PROMPT.startswith(ROLE.strip())
    assert NORMALIZE_CITATIONS_WORKFLOW in DEFAULT_SYSTEM_PROMPT
    assert "You are a patent document normalizer" not in NORMALIZE_CITATIONS_WORKFLOW
    assert "Workflow:" in NORMALIZE_CITATIONS_WORKFLOW


def test_normalize_docdb_citations_prompt(mcp_session):
    """Expose the shared workflow as a skill-style MCP prompt (no role)."""

    sample = "US 8,000,000 (Greenberg) teaches that..."

    async def _run() -> None:
        async with mcp_session() as session:
            listed = await session.list_prompts()
            prompt = next(p for p in listed.prompts if p.name == "normalize_docdb_citations")
            assert prompt.title == "Normalize DOCDB Citations"
            arg_names = [a.name for a in (prompt.arguments or [])]
            assert arg_names == ["text"]

            got = await session.get_prompt(
                "normalize_docdb_citations",
                {"text": sample},
            )
            assert len(got.messages) == 1
            assert got.messages[0].role == "user"
            content = got.messages[0].content
            text = content.text if hasattr(content, "text") else str(content)
            assert NORMALIZE_CITATIONS_WORKFLOW in text
            assert "You are a patent document normalizer" not in text
            assert "Document to normalize:" in text
            assert sample in text

    asyncio.run(_run())
