"""MCP server for DOCDB patent ID disambiguation.

Two modes selected at startup via environment variables:
- Hosted mode (default): calls the hosted HTTP API at DOCDB_API_URL.
- Local mode: opens the LMDB at DOCDB_LMDB_PATH directly (no HTTP).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
import lmdb
from mcp.server.fastmcp import FastMCP

from docdb_id.store.query import lookup_one
from docdb_id.store.schema import ALIAS_DB_NAME, DOCS_DB_NAME

logger = logging.getLogger("docdb_id.mcp")

mcp = FastMCP("DOCDB Disambiguator")

_env: lmdb.Environment | None = None
_docs_db: object = None
_alias_db: object | None = None


def _open_lmdb(lmdb_path: Path) -> None:
    global _env, _docs_db, _alias_db
    _env = lmdb.open(
        str(lmdb_path),
        readonly=True,
        subdir=lmdb_path.is_dir(),
        lock=False,
        readahead=False,
        max_dbs=3,
    )
    _docs_db = _env.open_db(DOCS_DB_NAME)
    try:
        _alias_db = _env.open_db(ALIAS_DB_NAME)
    except (lmdb.NotFoundError, lmdb.ReadonlyError):
        _alias_db = None


@mcp.tool()
def query_patent(cc: str, number: str) -> list[dict]:
    """Resolve a patent publication number to its canonical DOCDB record(s).

    Args:
        cc: Two-letter country code, e.g. "US" or "EP".
        number: Publication number without kind code, e.g. "20130143024".

    Returns:
        List of matching records. Each record has: docdb_id (full DOCDB ID
        including kind code, e.g. "US8000000B2"), inventor (full name in caps),
        date_publ (YYYYMMDD), family_id. Empty list means no match.
    """
    lmdb_path = os.environ.get("DOCDB_LMDB_PATH")
    if lmdb_path:
        return _lookup_local(cc, number)
    return _lookup_hosted(cc, number)


def _lookup_local(cc: str, number: str) -> list[dict]:
    global _env, _docs_db, _alias_db
    lmdb_path = Path(os.environ["DOCDB_LMDB_PATH"])
    if _env is None:
        _open_lmdb(lmdb_path)
    with _env.begin(write=False) as txn:
        return lookup_one(txn, _docs_db, _alias_db, cc, number)


def _lookup_hosted(cc: str, number: str) -> list[dict]:
    api_url = os.environ.get("DOCDB_API_URL", "").rstrip("/")
    if not api_url:
        raise RuntimeError("Either DOCDB_LMDB_PATH or DOCDB_API_URL must be set")
    resp = httpx.get(f"{api_url}/query", params={"cc": cc, "number": number}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
