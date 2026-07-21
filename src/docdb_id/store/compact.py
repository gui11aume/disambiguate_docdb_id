"""Compact an LMDB environment to its dense on-disk size."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import lmdb

logger = logging.getLogger("docdb_id.store.compact")


def compact_lmdb(src: Path, dest: Path) -> None:
    """Copy *src* to *dest* with unused pages dropped (`compact=True`).

    Args:
        src: Existing LMDB environment directory.
        dest: Destination directory (must not already exist as a usable env;
            removed if present).

    Raises:
        FileNotFoundError: If *src* does not exist.
    """
    if not src.exists():
        raise FileNotFoundError(f"LMDB path does not exist: {src}")
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.mkdir(parents=True)

    env = lmdb.open(
        str(src),
        subdir=src.is_dir(),
        readonly=True,
        lock=False,
        max_dbs=3,
    )
    try:
        env.copy(str(dest), compact=True)
    finally:
        env.close()


def compact_in_place(lmdb_path: Path) -> Path:
    """Replace *lmdb_path* with a compacted copy; preserve `*.done` sentinels.

    Uses a sibling temp directory, then renames into place. Requires roughly as
    much free disk as the dense size of the database.

    Args:
        lmdb_path: LMDB environment directory to compact in place.

    Returns:
        The same *lmdb_path* after replacement.
    """
    tmp = lmdb_path.with_name(f"{lmdb_path.name}.compact.tmp")
    old = lmdb_path.with_name(f"{lmdb_path.name}.precompact")

    before = (lmdb_path / "data.mdb").stat().st_size if (lmdb_path / "data.mdb").exists() else 0
    logger.info(f"compacting {lmdb_path} ({before} bytes) → {tmp}")
    compact_lmdb(lmdb_path, tmp)

    # Makefile sentinels like `.backfile.alias.done` (dotfiles; glob *.done misses them).
    for sentinel in lmdb_path.iterdir():
        if sentinel.is_file() and sentinel.name.endswith(".done"):
            shutil.copy2(sentinel, tmp / sentinel.name)

    if old.exists():
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    lmdb_path.rename(old)
    tmp.rename(lmdb_path)
    shutil.rmtree(old)

    after = (lmdb_path / "data.mdb").stat().st_size
    logger.info(f"compacted {lmdb_path}: {before} → {after} bytes")
    return lmdb_path
