"""Integration tests for alias maintenance during frontfile apply."""

from __future__ import annotations

import io
from pathlib import Path

import lmdb
import msgpack
import pytest

from docdb_id.alias.extract import alt_alias
from docdb_id.store.apply_frontfile import apply_changelog, load_applied_frontfile_parts
from docdb_id.store.schema import (
    ALIAS_DB_NAME,
    BUILD_STATUS_COMPLETE,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_ALIAS_BUILD_STATUS,
    META_KEY_CORE_BUILD_STATUS,
)


def _seed_lmdb(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    docs_db = env.open_db(DOCS_DB_NAME)
    alias_db = env.open_db(ALIAS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)

    key = b"US8888888"
    record = msgpack.packb([["US8888888A1", "Inventor", "20200101", "1"]], use_bin_type=True)
    alias = alt_alias(key, b"888888-1").alias
    assert alias is not None

    with env.begin(write=True) as txn:
        txn.put(key, record, db=docs_db)
        txn.put(alias, key, db=alias_db)
        txn.put(META_KEY_CORE_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
        txn.put(META_KEY_ALIAS_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
    env.close()


def _read_alias(lmdb_path: Path, alias: bytes) -> bytes | None:
    env = lmdb.open(str(lmdb_path), readonly=True, subdir=True, max_dbs=3)
    alias_db = env.open_db(ALIAS_DB_NAME)
    with env.begin(write=False) as txn:
        value = txn.get(alias, db=alias_db)
    env.close()
    return value


@pytest.fixture()
def lmdb_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.lmdb"
    _seed_lmdb(path)
    return path


def test_apply_create_adds_alternate_id_alias(lmdb_path: Path):
    changelog = io.BytesIO(
        b"US7777777\t000000010000000001\tC\tUS7777777A1\t777777-1\tInv\t20210101\t2\n"
    )
    apply_changelog(changelog, lmdb_path)

    assert _read_alias(lmdb_path, b"US7777771") == b"US7777777"


def test_apply_delete_removes_alternate_id_alias(lmdb_path: Path):
    alias = alt_alias(b"US8888888", b"888888-1").alias
    assert alias is not None
    changelog = io.BytesIO(
        b"US8888888\t000000010000000001\tD\tUS8888888A1\t888888-1\tInv\t20200101\t1\n"
    )
    apply_changelog(changelog, lmdb_path)

    assert _read_alias(lmdb_path, alias) is None


def test_apply_delete_keeps_alias_when_mapped_elsewhere(lmdb_path: Path):
    alias = alt_alias(b"US8888888", b"888888-1").alias
    assert alias is not None
    env = lmdb.open(str(lmdb_path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    alias_db = env.open_db(ALIAS_DB_NAME)
    with env.begin(write=True) as txn:
        txn.put(alias, b"OTHERKEY", db=alias_db)
    env.close()

    changelog = io.BytesIO(
        b"US8888888\t000000010000000001\tD\tUS8888888A1\t888888-1\tInv\t20200101\t1\n"
    )
    apply_changelog(changelog, lmdb_path)

    assert _read_alias(lmdb_path, alias) == b"OTHERKEY"


def test_load_applied_frontfile_parts_round_trips(lmdb_path: Path):
    changelog = io.BytesIO(
        b"US7777777\t000000010000000001\tC\tUS7777777A1\t777777-1\tInv\t20210101\t2\n"
    )
    apply_changelog(changelog, lmdb_path, applied_parts=["docdb_xml_202601_Amend_001"])

    assert load_applied_frontfile_parts(lmdb_path) == frozenset({"docdb_xml_202601_Amend_001"})


def test_load_applied_frontfile_parts_empty_for_missing_lmdb(tmp_path: Path):
    assert load_applied_frontfile_parts(tmp_path / "does_not_exist.lmdb") == frozenset()
