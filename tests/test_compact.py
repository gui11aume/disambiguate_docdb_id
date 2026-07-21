"""Tests for LMDB compact and write_map_size."""

from __future__ import annotations

import io
from pathlib import Path

import lmdb
import msgpack
import pytest

from docdb_id.store.compact import compact_in_place, compact_lmdb
from docdb_id.store.core import load_from_tsv
from docdb_id.store.schema import DEFAULT_MAP_HEADROOM, DOCS_DB_NAME, write_map_size


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
def loaded_lmdb(tmp_path: Path) -> Path:
    path = tmp_path / "src.lmdb"
    src = _tsv(("US8000000", "US8000000B2", "8000000", "KREITER", "2010-05-31", "123"))
    load_from_tsv(src, path, map_size=64 * 1024 * 1024)
    (path / ".backfile.alias.done").touch()
    return path


def test_write_map_size_is_file_size_plus_headroom(loaded_lmdb: Path):
    data_size = (loaded_lmdb / "data.mdb").stat().st_size
    assert write_map_size(loaded_lmdb) == data_size + DEFAULT_MAP_HEADROOM
    assert write_map_size(loaded_lmdb, headroom=1024) == data_size + 1024


def test_compact_lmdb_preserves_docs(loaded_lmdb: Path, tmp_path: Path):
    dest = tmp_path / "dest.lmdb"
    before = _read_docs(loaded_lmdb)
    compact_lmdb(loaded_lmdb, dest)
    assert _read_docs(dest) == before
    assert (dest / "data.mdb").stat().st_size <= (loaded_lmdb / "data.mdb").stat().st_size


def test_compact_in_place_preserves_sentinels_and_docs(loaded_lmdb: Path):
    before = _read_docs(loaded_lmdb)
    compact_in_place(loaded_lmdb)
    assert _read_docs(loaded_lmdb) == before
    assert (loaded_lmdb / ".backfile.alias.done").is_file()
    assert not loaded_lmdb.with_name(f"{loaded_lmdb.name}.compact.tmp").exists()
    assert not loaded_lmdb.with_name(f"{loaded_lmdb.name}.precompact").exists()
