"""Tests for store/core.py — backfile TSV to LMDB load."""

from __future__ import annotations

import io
from pathlib import Path

import lmdb
import msgpack
import pytest

from docdb_id.store.core import load_from_tsv
from docdb_id.store.schema import BUILD_STATUS_COMPLETE, DOCS_DB_NAME, META_DB_NAME, META_KEY_CORE_BUILD_STATUS


def _tsv(*rows: tuple) -> io.BytesIO:
    lines = [b"\t".join(f.encode() if isinstance(f, str) else f for f in row) + b"\n" for row in rows]
    return io.BytesIO(b"".join(lines))


def _read_docs(lmdb_path: Path) -> dict:
    env = lmdb.open(str(lmdb_path), readonly=True, subdir=True, max_dbs=3)
    docs_db = env.open_db(DOCS_DB_NAME)
    result = {}
    with env.begin(write=False) as txn:
        with txn.cursor(db=docs_db) as cur:
            for key, val in cur:
                result[key] = msgpack.unpackb(val, raw=False)
    env.close()
    return result


@pytest.fixture()
def lmdb_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lmdb"


def test_load_single_key(lmdb_path: Path):
    src = _tsv(("US8000000", "US8000000B2", "8000000", "KREITER", "2010-05-31", "123"))
    n_docs, n_keys = load_from_tsv(src, lmdb_path)
    assert n_docs == 1
    assert n_keys == 1
    docs = _read_docs(lmdb_path)
    assert b"US8000000" in docs
    assert docs[b"US8000000"][0][0] == "US8000000B2"


def test_load_deduplicates_same_docdb_id_under_one_key(lmdb_path: Path):
    src = _tsv(
        ("US8000000", "US8000000B2", "8000000", "KREITER", "2010-05-31", "123"),
        ("US8000000", "US8000000B2", "8000000", "KREITER", "2010-05-31", "123"),  # duplicate
    )
    n_docs, _ = load_from_tsv(src, lmdb_path)
    assert n_docs == 1  # deduped


def test_load_groups_multiple_docs_under_one_key(lmdb_path: Path):
    src = _tsv(
        ("US8000000", "US8000000A1", "8000000", "SMITH", "2005-01-01", "10"),
        ("US8000000", "US8000000B2", "8000000", "KREITER", "2010-05-31", "123"),
    )
    n_docs, n_keys = load_from_tsv(src, lmdb_path)
    assert n_docs == 2
    assert n_keys == 1
    docs = _read_docs(lmdb_path)
    assert len(docs[b"US8000000"]) == 2


def test_load_sets_build_status_complete(lmdb_path: Path):
    src = _tsv(("US1234567", "US1234567A1", "1234567", "INV", "20000101", "1"))
    load_from_tsv(src, lmdb_path)
    env = lmdb.open(str(lmdb_path), readonly=True, subdir=True, max_dbs=3)
    meta_db = env.open_db(META_DB_NAME)
    with env.begin(write=False) as txn:
        status = txn.get(META_KEY_CORE_BUILD_STATUS, db=meta_db)
    env.close()
    assert status == BUILD_STATUS_COMPLETE


def test_load_malformed_line_skipped(lmdb_path: Path):
    src = io.BytesIO(
        b"US8000000\tUS8000000B2\tKREITER\t2010-05-31\t123\n"  # only 5 cols, should be 6
        b"US9000000\tUS9000000A1\t9000000\tINV\t20000101\t1\n"
    )
    n_docs, n_keys = load_from_tsv(src, lmdb_path)
    assert n_keys == 1  # only the valid row loaded


def test_load_non_jp_key_with_leading_zero_raises(lmdb_path: Path):
    src = _tsv(("US0123456", "US0123456A1", "0123456", "INV", "20000101", "1"))
    with pytest.raises(ValueError, match="position 2"):
        load_from_tsv(src, lmdb_path)
