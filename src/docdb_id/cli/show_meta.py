"""Dump entries in the LMDB `meta` sub-DB.

Usage:
    python -m docdb_id.cli.show_meta <lmdb>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO

import lmdb

from docdb_id.store.schema import FRONTFILE_APPLIED_PREFIX, META_DB_NAME


def _decode(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return repr(value)


def show_meta(lmdb_path: Path, out: IO[str] | None = None) -> int:
    """Print key/value pairs from the meta sub-DB.

    Args:
        lmdb_path: Path to the LMDB environment.
        out: Output stream (defaults to sys.stdout).

    Returns:
        Exit code (0 on success, 1 if the path does not exist).
    """
    if out is None:
        out = sys.stdout

    if not lmdb_path.exists():
        print(f"{lmdb_path}: path does not exist", file=sys.stderr)
        return 1

    env = lmdb.open(
        str(lmdb_path),
        readonly=True,
        subdir=lmdb_path.is_dir(),
        lock=False,
        readahead=False,
        max_dbs=3,
    )
    try:
        try:
            meta_db = env.open_db(META_DB_NAME, create=False)
        except lmdb.NotFoundError:
            print(f"{lmdb_path}: no meta sub-DB", file=out)
            return 0

        rows: list[tuple[str, str]] = []
        with env.begin(write=False) as txn:
            with txn.cursor(db=meta_db) as cursor:
                for key, value in cursor:
                    rows.append((_decode(key), _decode(value)))

        frontfile_prefix = FRONTFILE_APPLIED_PREFIX.decode("utf-8")
        n_frontfile = sum(1 for key, _ in rows if key.startswith(frontfile_prefix))

        for key, value in sorted(rows):
            print(f"{key}\t{value}", file=out)

        print(f"{len(rows)} meta entries ({n_frontfile} frontfile_applied)", file=out)
    finally:
        env.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dump entries in the LMDB `meta` sub-DB.

    Args:
        argv: Command-line argument list. Must contain the LMDB path.

    Returns:
        Exit code (0 on success, 1 on usage error).
    """
    argv = sys.argv[1:] if argv is None else argv

    if len(argv) < 1:
        print("usage: python -m docdb_id.cli.show_meta <lmdb>", file=sys.stderr)
        return 1

    return show_meta(Path(argv[0]))


if __name__ == "__main__":
    sys.exit(main())
