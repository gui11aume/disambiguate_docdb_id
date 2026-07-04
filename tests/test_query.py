"""Tests for store/query.py — the core LMDB lookup path."""

from __future__ import annotations

import io
import json
from pathlib import Path

import lmdb
import msgpack
import pytest

from docdb_id.store.query import lookup_one, normalize, run_query
from docdb_id.store.schema import ALIAS_DB_NAME, DOCS_DB_NAME


def _seed(path: Path, docs: dict, aliases: dict | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    docs_db = env.open_db(DOCS_DB_NAME)
    alias_db = env.open_db(ALIAS_DB_NAME)
    with env.begin(write=True) as txn:
        for key, records in docs.items():
            txn.put(key.encode(), msgpack.packb(records, use_bin_type=True), db=docs_db)
        for alias, target in (aliases or {}).items():
            txn.put(alias.encode(), target.encode(), db=alias_db)
    env.close()


def _query(lmdb_path: Path, lines: list[str]) -> list[dict]:
    src = io.StringIO("\n".join(lines) + "\n")
    out = io.StringIO()
    run_query(lmdb_path, src, out)
    results = []
    for line in out.getvalue().splitlines():
        parts = line.split("\t", 2)
        results.append(json.loads(parts[2]))
    return results


@pytest.fixture()
def lmdb_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.lmdb"
    _seed(
        path,
        docs={"US8000000": [["US8000000B2", "KREITER", "2010-05-31", "123"]]},
        aliases={"US8888881": "US8888888", "US8888888": ""},
    )
    # Seed a docs entry for the alias target too
    path2 = tmp_path / "test2.lmdb"
    _seed(
        path2,
        docs={
            "US8000000": [["US8000000B2", "KREITER", "2010-05-31", "123"]],
            "US8888888": [["US8888888A1", "INV", "20200101", "1"]],
        },
        aliases={"US8888881": "US8888888"},
    )
    return path2


def test_normalize_strips_kind_and_leading_zeros():
    assert normalize('"US08000000B2"') == "US8000000"


def test_normalize_strips_quotes():
    assert normalize('"US8000000B2"') == "US8000000"


def test_normalize_no_kind():
    assert normalize("US8000000") == "US8000000"


def test_direct_docs_hit(lmdb_path: Path):
    results = _query(lmdb_path, ['context\t"US8000000B2"'])
    assert len(results) == 1
    assert results[0][0][0] == "US8000000B2"
    assert results[0][0][1] == "KREITER"


def test_alias_hit(lmdb_path: Path):
    results = _query(lmdb_path, ['context\t"US8888881"'])
    assert len(results) == 1
    assert results[0][0][0] == "US8888888A1"


def test_no_hit_returns_empty_list(lmdb_path: Path):
    results = _query(lmdb_path, ['context\t"US9999999B2"'])
    assert results == [[]]


def test_line_without_tab_is_skipped(lmdb_path: Path):
    src = io.StringIO("no tab here\n")
    out = io.StringIO()
    run_query(lmdb_path, src, out)
    assert out.getvalue() == ""


def test_jp_zero_fallback(tmp_path: Path):
    """JP13-char key with zero at pos 6 falls back to 12-char key."""
    path = tmp_path / "jp.lmdb"
    _seed(path, docs={"JP123456789012": [["JP123456789012A", "INV", "19990101", "9"]]})
    results = _query(path, ['ctx\t"JP1234560789012"'])
    assert results == [[]]


def test_missing_alias_db_falls_back_gracefully(tmp_path: Path):
    """Query against an LMDB with no alias sub-DB should not crash."""
    path = tmp_path / "noalias.lmdb"
    path.mkdir()
    env = lmdb.open(str(path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=1)
    docs_db = env.open_db(DOCS_DB_NAME)
    with env.begin(write=True) as txn:
        txn.put(b"US1234567", msgpack.packb([["US1234567A1", "INV", "20000101", "1"]], use_bin_type=True), db=docs_db)
    env.close()
    results = _query(path, ['ctx\t"US1234567A1"'])
    assert results[0][0][0] == "US1234567A1"


# --- lookup_one tests ---

@pytest.fixture()
def txn_dbs(lmdb_path: Path):
    """Yield (txn, docs_db, alias_db) for use in lookup_one tests."""
    env = lmdb.open(str(lmdb_path), readonly=True, subdir=True, lock=False, max_dbs=3)
    docs_db = env.open_db(DOCS_DB_NAME)
    alias_db = env.open_db(ALIAS_DB_NAME)
    with env.begin(write=False) as txn:
        yield txn, docs_db, alias_db
    env.close()


def test_lookup_one_direct_hit(txn_dbs):
    txn, docs_db, alias_db = txn_dbs
    results = lookup_one(txn, docs_db, alias_db, "US", "8000000")
    assert len(results) == 1
    assert results[0] == {"docdb_id": "US8000000B2", "inventor": "KREITER", "date_publ": "2010-05-31", "family_id": "123"}


def test_lookup_one_strips_leading_zeros(txn_dbs):
    txn, docs_db, alias_db = txn_dbs
    results = lookup_one(txn, docs_db, alias_db, "US", "08000000")
    assert len(results) == 1
    assert results[0]["docdb_id"] == "US8000000B2"


def test_lookup_one_alias_hit(txn_dbs):
    txn, docs_db, alias_db = txn_dbs
    results = lookup_one(txn, docs_db, alias_db, "US", "8888881")
    assert len(results) == 1
    assert results[0]["docdb_id"] == "US8888888A1"


def test_lookup_one_no_hit_returns_empty(txn_dbs):
    txn, docs_db, alias_db = txn_dbs
    assert lookup_one(txn, docs_db, alias_db, "US", "9999999") == []


def test_lookup_one_normalises_cc_case(txn_dbs):
    txn, docs_db, alias_db = txn_dbs
    results = lookup_one(txn, docs_db, alias_db, "us", "8000000")
    assert len(results) == 1
    assert results[0]["docdb_id"] == "US8000000B2"
