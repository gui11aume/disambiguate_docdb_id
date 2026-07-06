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
from pydantic import BaseModel, field_validator

from docdb_id.country_codes import VALID_CC
from docdb_id.store.query import lookup_one
from docdb_id.store.schema import (
    ALIAS_DB_NAME,
    DEFAULT_MAP_SIZE,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_CORE_LAST_UPDATED,
    META_KEY_FRONTFILE_LAST_APPLIED,
)

logger = logging.getLogger("docdb_id.api")

MAX_BATCH = 10_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    lmdb_path = Path(os.environ["DOCDB_LMDB_PATH"])
    # This env is opened once and held for the process lifetime, unlike every
    # other (short-lived, one-shot) reader in this codebase. Without an
    # explicit map_size, LMDB defaults readers to a small reservation that
    # only covers the DB's size at open time; a `make apply-frontfile` run on
    # the host growing the DB afterwards then makes every subsequent read
    # fail with MDB_MAP_RESIZED until this process restarts. Matching the
    # writers' DEFAULT_MAP_SIZE upfront avoids that: it's a virtual address
    # space reservation, not real disk usage, so requesting it eagerly is free.
    env = lmdb.open(
        str(lmdb_path),
        map_size=DEFAULT_MAP_SIZE,
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
    app.state.env = env
    app.state.docs_db = docs_db
    app.state.alias_db = alias_db
    app.state.meta_db = meta_db
    yield
    env.close()


app = FastAPI(title="DOCDB Disambiguator", lifespan=lifespan)


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


# Known-stable canary record queried by /health. A plain "process is up" check
# would have stayed green through the MDB_MAP_RESIZED incident, since it never
# touched the LMDB env; querying a fixed, real docdb_id catches that class of
# failure instead of just reporting the process as alive.
HEALTH_CHECK_CC = "US"
HEALTH_CHECK_NUMBER = "8000000"
HEALTH_CHECK_DOCDB_ID = "US8000000B2"


@app.api_route("/health", methods=["GET", "HEAD"])
async def health() -> dict:
    env = app.state.env
    docs_db = app.state.docs_db
    alias_db = app.state.alias_db

    def _sync() -> list[dict]:
        with env.begin(write=False) as txn:
            return lookup_one(txn, docs_db, alias_db, HEALTH_CHECK_CC, HEALTH_CHECK_NUMBER)

    try:
        records = await anyio.to_thread.run_sync(_sync)
    except lmdb.Error as exc:
        raise HTTPException(status_code=503, detail=f"lmdb error: {exc}") from exc

    if not any(r["docdb_id"] == HEALTH_CHECK_DOCDB_ID for r in records):
        raise HTTPException(
            status_code=503,
            detail=f"expected {HEALTH_CHECK_DOCDB_ID} for {HEALTH_CHECK_CC}{HEALTH_CHECK_NUMBER}, got {records}",
        )
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
    uvicorn.run("docdb_id.api.server:app", host="0.0.0.0", port=8000)
