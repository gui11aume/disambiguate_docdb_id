"""MCP server for DOCDB patent ID disambiguation.

Calls the hosted HTTP API at DOCDB_API_URL.
"""

from __future__ import annotations

import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from docdb_id.agent.default_prompt import NORMALIZE_CITATIONS_WORKFLOW

logger = logging.getLogger("docdb_id.mcp")

mcp = FastMCP("DOCDB Disambiguator", host="0.0.0.0", port=8001)

MAX_MCP_BATCH = 50


class ItemToResolve(BaseModel):
    """A single patent publication number to resolve."""

    cc: str = Field(description='Two-letter DOCDB country code, e.g. "US", "EP".')
    number: str = Field(
        description=(
            "Publication number without kind code or country prefix; "
            "digits and letters only."
        ),
    )


@mcp.prompt(
    name="normalize_docdb_citations",
    title="Normalize DOCDB Citations",
    description=(
        "Append canonical DOCDB IDs to patent references in free text. "
        "Mirrors the DOCDB Resolver agent skill: call resolve_docdb_id, "
        "match inventor/date context, and rewrite the document in place."
    ),
)
def normalize_docdb_citations() -> str:
    """Claude-skill-style workflow to resolve DOCDB citations."""
    return f"{NORMALIZE_CITATIONS_WORKFLOW}"


@mcp.tool()
def resolve_docdb_id(items: list[ItemToResolve]) -> list[dict]:
    """Resolve patent publication numbers to canonical DOCDB record(s).

    Pass all distinct references in one call (max 50 items). Each item is
    looked up independently; results are returned in the same order, keyed by
    the input cc and number.

    IMPORTANT — strip the kind code before calling:
        "US8000000B2"  → cc="US",  number="8000000"
        "EP1234567A1"  → cc="EP",  number="1234567"
        "WO2013143024" → cc="WO",  number="2013143024"
    The kind code (trailing letter+digit suffix like B2, A1, A2, U1) is NEVER
    part of the number argument. Passing it causes an empty result, not an error.

    Also strip formatting: "US 8,000,000" → cc="US", number="8000000".

    Example:
        resolve_docdb_id(items=[
            {"cc": "US", "number": "8000000"},
            {"cc": "EP", "number": "1234567"},
            {"cc": "WO", "number": "2013143024"},
        ])

    Leading zeros in the number are ignored: "08000000" and "8000000" are
    equivalent.

    If an item's results list is empty and error is null:
      1. Check that you stripped the kind code (most common mistake).
      2. Consider common transcription errors: O/0, I/1, S/5, B/8.
         Try plausible substitutions in the number.
      3. Use all context available to you (inventor name, year) to
         reconstruct the most likely number and retry.

    Processing the output:
      Each item returns the first inventor and publication date. These map
      directly onto how patents are cited in practice: "Greenberg et al. (2011)"
      should match inventor "ROBERT J. GREENBERG" and date_publ starting with
      "2011". If you get multiple records for one item, compare inventor names
      and publication dates to select the most likely match. The tool gives you
      candidates, not a verdict.

    Args:
        items: One or more lookups (max 50). Each has:
          - cc: Two-letter DOCDB country code, e.g. "US", "EP", "WO".
          - number: Publication number without kind code or country prefix,
            digits and letters only (no hyphens, spaces, or slashes).

    Returns:
        One object per input item, in the same order, each with:
          - cc, number: echoed from the request
          - results: list of matching records, each with:
              - docdb_id:  full DOCDB ID including kind code, e.g. "US8000000B2"
              - inventor:  first inventor full name in caps
              - date_publ: publication date as YYYYMMDD
              - family_id: DOCDB patent family ID
            Multiple records mean document variants (e.g. A1 and B2).
            Empty results with error null means no match — not an error.
          - error: null on success, or an inline validation error:
              - "cc_does_not_exist": unrecognized DOCDB country code
              - "number_is_not_alnum": illegal characters in number
    """
    if not items:
        raise ValueError("items must be non-empty")
    if len(items) > MAX_MCP_BATCH:
        raise ValueError(f"max {MAX_MCP_BATCH} items per call")

    api_url = os.environ.get("DOCDB_API_URL", "").rstrip("/")
    if not api_url:
        raise RuntimeError("DOCDB_API_URL must be set")

    payload = {"items": [item.model_dump() for item in items]}
    resp = httpx.post(f"{api_url}/batch", json=payload, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    """Run the MCP server with stdio transport."""
    mcp.run()


def main_http() -> None:
    """Run as a standalone HTTP server (for hosted deployment)."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
