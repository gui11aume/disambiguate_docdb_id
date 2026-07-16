"""Load the `alias` sub-DB from a sorted 2-column TSV.

The input must be sorted ascending on column 1 with `LC_ALL=C` and contain
exactly one row per alias (see the Makefile's alias stage for the full sort +
collapse pipeline).

Usage:
    python -m docdb_id.cli.alias_load <lmdb-path> [<sorted-2col-tsv>]   # else stdin
"""

from __future__ import annotations

import sys
from pathlib import Path

from docdb_id.store.alias import _Collision, load_alias


def main(argv: list[str] | None = None) -> int:
    """Load the `alias` sub-DB from a sorted 2-column TSV.

    Args:
        argv: Command-line argument list. First element is the LMDB path; optional
            second element is a path to a sorted 2-column TSV file (reads stdin if
            omitted).

    Returns:
        Exit code (0 on success, 1 on usage error, 2 on alias collision).

    Raises:
        _Collision: If a key collision is detected during alias loading.
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 1:
        print("usage: python -m docdb_id.cli.alias_load <lmdb-path> [<sorted-2col-tsv>]", file=sys.stderr)
        return 1

    lmdb_path = Path(argv[0])
    src_path = Path(argv[1]) if len(argv) > 1 else None

    try:
        if src_path is not None:
            with src_path.open("rb") as fh:
                n, n_skipped_docs = load_alias(fh, lmdb_path)
        else:
            n, n_skipped_docs = load_alias(sys.stdin.buffer, lmdb_path)
    except _Collision as exc:
        print(f"\nalias build aborted: {exc}", file=sys.stderr)
        return 2

    print(
        f"\nalias: {n:,} aliases -> {lmdb_path} ({n_skipped_docs:,} skipped; already in docs)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
