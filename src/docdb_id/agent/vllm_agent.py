"""Patent disambiguation agent using vLLM with tool calling.

Requires a running vLLM server:

    vllm serve Qwen/Qwen3-35B-A3B-FP8 \\
        --enable-auto-tool-choice \\
        --tool-call-parser hermes

Environment variables:
    VLLM_BASE_URL:  vLLM server URL (default: http://localhost:8000/v1)
    DOCDB_API_URL:  DOCDB API URL (default: https://docdb.sarl-graip.fr)
    VLLM_MODEL:     model name as served by vLLM
                    (default: Qwen/Qwen3-35B-A3B-FP8)
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from openai import OpenAI

logger = logging.getLogger(__name__)

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
DOCDB_API_URL = os.environ.get("DOCDB_API_URL", "https://docdb.sarl-graip.fr")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-35B-A3B-FP8")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_patent",
            "description": (
                "Resolve a patent publication number to its canonical DOCDB record(s).\n"
                "\n"
                "IMPORTANT — strip the kind code before calling:\n"
                '    "US8000000B2"  → cc="US",  number="8000000"\n'
                '    "EP1234567A1"  → cc="EP",  number="1234567"\n'
                '    "WO2013143024" → cc="WO",  number="2013143024"\n'
                "The kind code (trailing letter+digit suffix like B2, A1, A2, U1) is NEVER\n"
                "part of the number argument. Passing it causes an empty result, not an error.\n"
                "\n"
                'Also strip formatting: "US 8,000,000" → cc="US", number="8000000".\n'
                "\n"
                "Leading zeros in the number are ignored: '08000000' and '8000000' are equivalent.\n"
                "\n"
                "If you get an empty list:\n"
                "  1. Check that you stripped the kind code (most common mistake).\n"
                "  2. Consider common transcription errors: O/0, I/1, S/5, B/8.\n"
                "     Try plausible substitutions in the number.\n"
                "  3. Use all context available to you (inventor name, year) to\n"
                "     reconstruct the most likely number and retry.\n"
                "\n"
                "Processing the output:\n"
                "  The tool returns the first inventor and publication date. These map\n"
                "  directly onto how patents are cited in practice: 'Greenberg et al. (2011)'\n"
                "  should match inventor 'ROBERT J. GREENBERG' and date_publ starting with '2011'.\n"
                "  If you get multiple records, compare inventor names and publication dates\n"
                "  to select the most likely match. The tool gives you candidates, not a verdict.\n"
                "\n"
                "Error codes (returned inline, not as exceptions):\n"
                "  - cc_does_not_exist: cc is not a recognized DOCDB country code.\n"
                "  - number_is_not_alnum: number contains illegal characters — strip them.\n"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cc": {
                        "type": "string",
                        "description": (
                            "Two-letter DOCDB country code, e.g. 'US', 'EP', 'WO', "
                            "'DE', 'JP', 'FR', 'GB', 'CN', 'KR'. Exactly 2 characters."
                        ),
                    },
                    "number": {
                        "type": "string",
                        "description": (
                            "Publication number without kind code or country prefix. "
                            "Digits and letters only — no hyphens, spaces, commas, or slashes."
                        ),
                    },
                },
                "required": ["cc", "number"],
            },
        },
    }
]


def _call_query_patent(cc: str, number: str) -> list[dict]:
    url = f"{DOCDB_API_URL.rstrip('/')}/query"
    resp = httpx.get(url, params={"cc": cc, "number": number}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _dispatch_tool(name: str, arguments: str) -> str:
    args = json.loads(arguments)
    if name == "query_patent":
        result = _call_query_patent(args["cc"], args["number"])
        return json.dumps(result)
    raise ValueError(f"unknown tool: {name}")


def run(user_message: str, system_prompt: str | None = None) -> str:
    """Run a single agentic turn and return the final text response.

    Args:
        user_message: The user's request.
        system_prompt: Optional system prompt to set context.

    Returns:
        The model's final text response after all tool calls are resolved.
    """
    client = OpenAI(base_url=VLLM_BASE_URL, api_key="x")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            return msg.content

        for tc in msg.tool_calls:
            logger.debug("tool call: %s(%s)", tc.function.name, tc.function.arguments)
            result = _dispatch_tool(tc.function.name, tc.function.arguments)
            logger.debug("tool result: %s", result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Patent disambiguation agent")
    parser.add_argument("query", help='e.g. "Who invented US 8,000,000?"')
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    answer = run(args.query)
    print(answer)


if __name__ == "__main__":
    main()
