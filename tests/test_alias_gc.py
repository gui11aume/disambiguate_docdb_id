"""Tests for the dangling-alias prune (alias garbage collection)."""

from __future__ import annotations

from pathlib import Path

import lmdb
import msgpack
import pytest

from docdb_id.store.alias import prune_orphan_aliases
from docdb_id.store.schema import (
    ALIAS_DB_NAME,
    BUILD_STATUS_COMPLETE,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_ALIAS_BUILD_STATUS,
    META_KEY_ALIAS_NO_DANGLING,
)


def _seed_lmdb(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    docs_db = env.open_db(DOCS_DB_NAME)
    alias_db = env.open_db(ALIAS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)

    record = msgpack.packb([["US8888888A1", "Inventor", "20200101", "1"]], use_bin_type=True)
    with env.begin(write=True) as txn:
        txn.put(b"US8888888", record, db=docs_db)
        # Live alias: target key exists in docs.
        txn.put(b"US8888881", b"US8888888", db=alias_db)
        # Dangling alias: target key is absent from docs.
        txn.put(b"US7777771", b"US7777777", db=alias_db)
        txn.put(META_KEY_ALIAS_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
    env.close()


def _read(lmdb_path: Path, db_name: bytes, key: bytes) -> bytes | None:
    env = lmdb.open(str(lmdb_path), readonly=True, subdir=True, max_dbs=3)
    db = env.open_db(db_name)
    with env.begin(write=False) as txn:
        value = txn.get(key, db=db)
    env.close()
    return value


@pytest.fixture()
def lmdb_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.lmdb"
    _seed_lmdb(path)
    return path


def test_prune_removes_dangling_alias_keeps_live(lmdb_path: Path):
    n_scanned, n_deleted = prune_orphan_aliases(lmdb_path)

    assert n_scanned == 2
    assert n_deleted == 1
    assert _read(lmdb_path, ALIAS_DB_NAME, b"US8888881") == b"US8888888"
    assert _read(lmdb_path, ALIAS_DB_NAME, b"US7777771") is None


def test_prune_sets_no_dangling_meta_flag(lmdb_path: Path):
    assert _read(lmdb_path, META_DB_NAME, META_KEY_ALIAS_NO_DANGLING) is None

    prune_orphan_aliases(lmdb_path)

    assert _read(lmdb_path, META_DB_NAME, META_KEY_ALIAS_NO_DANGLING) is not None
    assert _read(lmdb_path, META_DB_NAME, META_KEY_ALIAS_BUILD_STATUS) == BUILD_STATUS_COMPLETE


def test_prune_is_idempotent(lmdb_path: Path):
    prune_orphan_aliases(lmdb_path)
    n_scanned, n_deleted = prune_orphan_aliases(lmdb_path)

    assert n_scanned == 1
    assert n_deleted == 0
