"""Patent disambiguation agent using vLLM with tool calling.

Requires a running vLLM server:

    vllm serve Qwen/Qwen3.6-35B-A3B-FP8 \\
        --reasoning-parser qwen3 \\
        --enable-auto-tool-choice \\
        --tool-call-parser qwen3_coder \\
        --tensor-parallel 2 \\
        --max-num-seqs 1 \\
        --gpu-memory-utilization 0.95 \\
        --kv-cache-dtype fp8

Environment variables:
    VLLM_BASE_URL:  vLLM server URL (default: http://localhost:8000/v1)
    DOCDB_API_URL:  DOCDB API URL (default: https://docdb.sarl-graip.fr)
    VLLM_MODEL:     model name as served by vLLM
                    (default: Qwen/Qwen3.6-35B-A3B-FP8)

Example usage:
    uv run docdb-agent --debug "US 8,000,000 (Greenberg) teaches that..."
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

from .default_prompt import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
DOCDB_API_URL = os.environ.get("DOCDB_API_URL", "https://docdb.sarl-graip.fr")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3.6-35B-A3B-FP8")

SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


def _mcp_url() -> str:
    """Return the DOCDB MCP streamable-HTTP endpoint URL."""
    return f"{DOCDB_API_URL.rstrip('/')}/mcp"


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


async def _run_async(user_message: str, system_prompt: str | None) -> str:
    """Run the agent loop against the DOCDB MCP server.

    Args:
        user_message: The user's request.
        system_prompt: Optional system prompt to set context.

    Returns:
        The model's final text response after all tool calls are resolved.
    """
    client = OpenAI(base_url=VLLM_BASE_URL, api_key="x")

    async with streamablehttp_client(_mcp_url()) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [_mcp_tool_to_openai(t) for t in listed.tools]

            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_message})

            while True:
                response = client.chat.completions.create(
                    model=MODEL,
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


def run(user_message: str, system_prompt: str | None = None) -> str:
    """Run a single agentic turn and return the final text response.

    Args:
        user_message: The user's request.
        system_prompt: Optional system prompt to set context.

    Returns:
        The model's final text response after all tool calls are resolved.
    """
    return asyncio.run(_run_async(user_message, system_prompt))


def main() -> None:
    """CLI entry point for the patent disambiguation agent."""
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Patent disambiguation agent")
    parser.add_argument("query", help='e.g. "Who invented US 7,000,000?"')
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    answer = run(args.query, system_prompt=SYSTEM_PROMPT)
    print(answer)


if __name__ == "__main__":
    main()
