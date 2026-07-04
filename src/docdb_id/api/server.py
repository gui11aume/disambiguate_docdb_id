"""FastAPI server for DOCDB patent ID disambiguation."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
import lmdb
from fastapi import FastAPI, HTTPException, Query
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, field_validator

from docdb_id.country_codes import VALID_CC
from docdb_id.store.query import lookup_one
from docdb_id.store.schema import (
    ALIAS_DB_NAME,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_CORE_LAST_UPDATED,
    META_KEY_FRONTFILE_LAST_APPLIED,
)

logger = logging.getLogger("docdb_id.api")

MAX_BATCH = 10_000

# Module-level LMDB handles shared between REST and MCP handlers.
# Set during lifespan before any requests are served.
_env: lmdb.Environment | None = None
_docs_db: object = None
_alias_db: object | None = None

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
    with _env.begin(write=False) as txn:
        return lookup_one(txn, _docs_db, _alias_db, cc, number)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _env, _docs_db, _alias_db
    lmdb_path = Path(os.environ["DOCDB_LMDB_PATH"])
    env = lmdb.open(
        str(lmdb_path),
        readonly=True,
        subdir=lmdb_path.is_dir(),
        lock=False,
        readahead=False,
        max_dbs=3,
    )
    docs_db = env.open_db(DOCS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)
    try:
        alias_db = env.open_db(ALIAS_DB_NAME)
    except (lmdb.NotFoundError, lmdb.ReadonlyError):
        alias_db = None
    _env = env
    _docs_db = docs_db
    _alias_db = alias_db
    app.state.env = env
    app.state.docs_db = docs_db
    app.state.alias_db = alias_db
    app.state.meta_db = meta_db
    yield
    env.close()


app = FastAPI(title="DOCDB Disambiguator", lifespan=lifespan)
app.mount("/mcp/", mcp.streamable_http_app())


class Record(BaseModel):
    docdb_id: str
    inventor: str
    date_publ: str
    family_id: str


class BatchItem(BaseModel):
    cc: str
    number: str


class BatchRequest(BaseModel):
    items: list[BatchItem]

    @field_validator("items")
    @classmethod
    def check_max(cls, v: list[BatchItem]) -> list[BatchItem]:
        if len(v) > MAX_BATCH:
            raise ValueError(f"max {MAX_BATCH} items per request")
        return v


class BatchItemResult(BaseModel):
    cc: str
    number: str
    results: list[Record]
    error: str | None = None


def _validate_cc(cc: str) -> str | None:
    if cc.upper().encode() not in VALID_CC:
        return "cc_does_not_exist"
    return None


def _validate_number(number: str) -> str | None:
    if not (number.isascii() and number.isalnum()):
        return "number_is_not_alnum"
    return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict:
    env = app.state.env
    docs_db = app.state.docs_db
    meta_db = app.state.meta_db

    def _sync() -> dict:
        with env.begin(write=False) as txn:
            candidates = [
                v.decode()
                for v in (
                    txn.get(META_KEY_CORE_LAST_UPDATED, db=meta_db),
                    txn.get(META_KEY_FRONTFILE_LAST_APPLIED, db=meta_db),
                )
                if v is not None
            ]
            last_updated = max(candidates) if candidates else None
            key_count = txn.stat(docs_db)["entries"]
        return {"last_updated": last_updated, "key_count": key_count}

    return await anyio.to_thread.run_sync(_sync)


@app.get("/query")
async def query(
    cc: Annotated[str, Query(min_length=2, max_length=2)],
    number: Annotated[str, Query(min_length=1)],
) -> list[Record]:
    if err := _validate_cc(cc):
        raise HTTPException(status_code=422, detail=err)
    if err := _validate_number(number):
        raise HTTPException(status_code=422, detail=err)

    env = app.state.env
    docs_db = app.state.docs_db
    alias_db = app.state.alias_db

    def _sync() -> list[dict]:
        with env.begin(write=False) as txn:
            return lookup_one(txn, docs_db, alias_db, cc, number)

    records = await anyio.to_thread.run_sync(_sync)
    return [Record(**r) for r in records]


@app.post("/batch")
async def batch(req: BatchRequest) -> list[BatchItemResult]:
    env = app.state.env
    docs_db = app.state.docs_db
    alias_db = app.state.alias_db

    def _sync() -> list[BatchItemResult]:
        results = []
        with env.begin(write=False) as txn:
            for item in req.items:
                if err := _validate_cc(item.cc):
                    results.append(BatchItemResult(cc=item.cc, number=item.number, results=[], error=err))
                    continue
                if err := _validate_number(item.number):
                    results.append(BatchItemResult(cc=item.cc, number=item.number, results=[], error=err))
                    continue
                records = lookup_one(txn, docs_db, alias_db, item.cc, item.number)
                results.append(BatchItemResult(
                    cc=item.cc,
                    number=item.number,
                    results=[Record(**r) for r in records],
                ))
        return results

    return await anyio.to_thread.run_sync(_sync)


def main() -> None:
    import uvicorn
    uvicorn.run("docdb_id.api.server:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
