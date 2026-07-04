"""MCP server for DOCDB patent ID disambiguation.

Calls the hosted HTTP API at DOCDB_API_URL.
"""

from __future__ import annotations

import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("docdb_id.mcp")

mcp = FastMCP("DOCDB Disambiguator")


@mcp.tool()
def resolve_docdb_id(cc: str, number: str) -> list[dict]:
    """Resolve a patent publication number to its canonical DOCDB record(s).

    IMPORTANT — strip the kind code before calling:
        "US8000000B2"  → cc="US",  number="8000000"
        "EP1234567A1"  → cc="EP",  number="1234567"
        "WO2013143024" → cc="WO",  number="2013143024"
    The kind code (trailing letter+digit suffix like B2, A1, A2, U1) is NEVER
    part of the number argument. Passing it causes an empty result, not an error.

    Also strip formatting: "US 8,000,000" → cc="US", number="8000000".

    Leading zeros in the number are ignored: "08000000" and "8000000" are
    equivalent.

    Args:
        cc: Two-letter DOCDB country code, e.g. "US", "EP", "WO", "DE", "JP",
            "FR", "GB", "CN", "KR". Must be exactly 2 characters.
        number: Publication number without kind code or country prefix, digits
            and letters only (no hyphens, spaces, or slashes).

    Returns:
        List of matching records, each with:
          - docdb_id:  full DOCDB ID including kind code, e.g. "US8000000B2"
          - inventor:  first inventor full name in caps, e.g. "ROBERT J. GREENBERG"
          - date_publ: publication date as YYYYMMDD, e.g. "20110816"
          - family_id: DOCDB patent family ID, e.g. "39183031"
        Multiple records mean the same publication number has several document
        variants (e.g. an A1 and a B2 publication of the same application).
        Empty list means no match — not an error.

    If you get an empty list:
      1. Check that you stripped the kind code (most common mistake).
      2. Consider common transcription errors in the source material: O/0, I/1,
         S/5, B/8. Try plausible substitutions in the number.
      3. Use all context available to you (inventor name, year) to reconstruct
         the most likely number and retry.

    Processing the output:
        The tool returns the first inventor and publication date. These map
        directly onto the way patents are cited in practice: "Greenberg",
        "Greenberg et al.", or "Greenberg et al. (2011)" in a source document
        should match inventor "ROBERT J. GREENBERG" and date_publ starting
        with "2011". Use that correspondence to verify the match.
        If you get multiple records, compare inventor names and publication dates
        across the candidates to select the most likely one.
        In all cases you must decide: the tool gives you candidates, not a verdict.

    Error codes returned by the server (not exceptions):
      - "cc_does_not_exist": cc is not a recognized DOCDB country code — check
        spelling or try the ISO 2-letter code for the country.
      - "number_is_not_alnum": number contains illegal characters such as
        hyphens, slashes, or spaces — strip them before retrying.
    """
    api_url = os.environ.get("DOCDB_API_URL", "").rstrip("/")
    if not api_url:
        raise RuntimeError("DOCDB_API_URL must be set")
    resp = httpx.get(f"{api_url}/query", params={"cc": cc, "number": number}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
