"""Prune dangling aliases from an existing LMDB.

Scans the `alias` sub-DB and removes every entry whose target key is no longer
present in the `docs` sub-DB. On success the `alias_no_dangling` meta key is set
to the verification timestamp. Safe to run repeatedly.

Usage:
    python -m docdb_id.cli.alias_gc <lmdb-path>
"""

from __future__ import annotations

import sys
from pathlib import Path

from docdb_id.store.alias import prune_orphan_aliases


def main(argv: list[str] | None = None) -> int:
    """Prune dangling aliases from an existing LMDB.

    Args:
        argv: Command-line argument list. Must contain exactly one element: the
            path to the LMDB.

    Returns:
        Exit code (0 on success, 1 on usage error).
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m docdb_id.cli.alias_gc <lmdb-path>", file=sys.stderr)
        return 1

    lmdb_path = Path(argv[0])
    n_scanned, n_deleted = prune_orphan_aliases(lmdb_path)

    print(
        f"\nalias prune: {n_scanned:,} scanned, {n_deleted:,} dangling alias(es) removed -> {lmdb_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
