"""Citation-cleaning agent for the hosted web app.

Bridges a real MCP client session against `docdb.sarl-graip.fr/mcp` with
Scaleway's OpenAI-compatible hosted LLM. Kept separate from
`docdb_id.agent.vllm_agent`: that module talks to a self-hosted vLLM server
and dispatches its tool by hand-written HTTP call, while this one is a genuine
MCP client speaking to the public MCP endpoint - different enough integration
patterns that sharing an abstraction isn't worth it. Both reuse
`DEFAULT_SYSTEM_PROMPT`.

Environment variables:
    DOCDB_MCP_URL:      DOCDB MCP streamable-HTTP endpoint
                        (default: https://docdb.sarl-graip.fr/mcp)
    SCALEWAY_API_KEY:   API key for Scaleway's hosted LLM
    SCALEWAY_BASE_URL:  OpenAI-compatible base URL for Scaleway's hosted LLM
    SCALEWAY_MODEL:     model name (default: qwen3.6-35b-a3b)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI

from docdb_id.agent.default_prompt import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("docdb_id.web.mcp_agent")

DOCDB_MCP_URL = os.environ.get("DOCDB_MCP_URL", "https://docdb.sarl-graip.fr/mcp")
SCALEWAY_API_KEY = os.environ.get("SCALEWAY_API_KEY", "")
SCALEWAY_BASE_URL = os.environ.get("SCALEWAY_BASE_URL", "")
SCALEWAY_MODEL = os.environ.get("SCALEWAY_MODEL", "qwen3.6-35b-a3b")


def _mcp_tool_to_openai(tool: Any) -> dict:
    """Convert an MCP tool definition to OpenAI function-calling format.

    Args:
        tool: An MCP `Tool` from `list_tools`.

    Returns:
        OpenAI-compatible tool dict.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def _tool_result_payload(result: Any) -> Any:
    """Extract a JSON-serializable payload from an MCP `CallToolResult`.

    Args:
        result: MCP tool call result.

    Returns:
        Structured tool payload when available, otherwise parsed text content.

    Raises:
        RuntimeError: If the MCP tool reported an error or returned no usable payload.
    """
    if result.isError:
        texts = [c.text for c in result.content if hasattr(c, "text")]
        detail = "; ".join(texts) if texts else "unknown MCP tool error"
        raise RuntimeError(f"MCP tool failed: {detail}")

    structured = result.structuredContent
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    if structured is not None:
        return structured

    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        return json.loads(text)

    raise RuntimeError("MCP tool returned no usable payload")


async def run_agent(
    session: ClientSession,
    openai_client: OpenAI,
    model: str,
    user_message: str,
    system_prompt: str | None = None,
) -> str:
    """Run a tool-calling loop against an already-initialized MCP session.

    Kept independent of transport setup so tests can pass a mock
    `ClientSession` instead of opening a real connection.

    Args:
        session: An initialized MCP `ClientSession`.
        openai_client: OpenAI-compatible client (e.g. pointed at Scaleway).
        model: Model name to request completions from.
        user_message: The user's request (citation text to clean).
        system_prompt: Optional system prompt to set context.

    Returns:
        The model's final text response after all tool calls are resolved.
    """
    listed = await session.list_tools()
    tools = [_mcp_tool_to_openai(t) for t in listed.tools]

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    while True:
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            return msg.content

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("tool call: %s(%s)", tc.function.name, tc.function.arguments)
            result = await session.call_tool(tc.function.name, args)
            payload = _tool_result_payload(result)
            content = json.dumps(payload)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("tool result: %s", content)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": content,
            })


async def clean_text(text: str) -> str:
    """Replace patent citations in `text` with canonical DOCDB IDs.

    Opens a real MCP client session against `DOCDB_MCP_URL` and a Scaleway
    chat-completions client, then runs the tool-calling loop.

    Args:
        text: Free text containing patent citations (already length-checked
            by the caller).

    Returns:
        The text with citations replaced by canonical DOCDB IDs.
    """
    openai_client = OpenAI(base_url=SCALEWAY_BASE_URL, api_key=SCALEWAY_API_KEY)

    async with streamablehttp_client(DOCDB_MCP_URL) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await run_agent(
                session,
                openai_client,
                SCALEWAY_MODEL,
                text,
                system_prompt=DEFAULT_SYSTEM_PROMPT,
            )


def clean_text_sync(text: str) -> str:
    """Synchronous wrapper around `clean_text` for non-async callers.

    Args:
        text: Free text containing patent citations.

    Returns:
        The text with citations replaced by canonical DOCDB IDs.
    """
    return asyncio.run(clean_text(text))
