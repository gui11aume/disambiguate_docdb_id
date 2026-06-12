"""Tests for the show_meta CLI helper."""

from __future__ import annotations

import io
from pathlib import Path

import lmdb

from docdb_id.cli.show_meta import show_meta
from docdb_id.store.schema import (
    FRONTFILE_APPLIED_PREFIX,
    META_DB_NAME,
    META_KEY_CORE_BUILD_STATUS,
)


def _seed_meta(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    meta_db = env.open_db(META_DB_NAME)
    with env.begin(write=True) as txn:
        txn.put(META_KEY_CORE_BUILD_STATUS, b"complete", db=meta_db)
        txn.put(FRONTFILE_APPLIED_PREFIX + b"010096", b"2026-06-12T01:33:40+00:00", db=meta_db)
    env.close()


def test_show_meta_shows_frontfile_applied(tmp_path: Path):
    lmdb_path = tmp_path / "test.lmdb"
    _seed_meta(lmdb_path)
    out = io.StringIO()

    assert show_meta(lmdb_path, out) == 0

    output = out.getvalue()
    assert "core_build_status\tcomplete" in output
    assert "frontfile_applied:010096\t2026-06-12T01:33:40+00:00" in output
    assert "2 meta entries (1 frontfile_applied)" in output
