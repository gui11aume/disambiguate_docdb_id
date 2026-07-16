"""Load a sorted backfile TSV into the LMDB `docs` sub-DB.

Reads from stdin (or a file passed as the second argument). The input must
already be sorted by the first column with `LC_ALL=C`.

Usage:
    # Typical pipeline:
    ... | LC_ALL=C sort -t $'\t' -k1,1 | python -m docdb_id.cli.core out/docdb.lmdb

    # Or read from a pre-sorted file:
    python -m docdb_id.cli.core out/docdb.lmdb stage/sorted.tsv
"""

from __future__ import annotations

import sys
from pathlib import Path

from docdb_id.store.core import load_from_tsv


def main(argv: list[str] | None = None) -> int:
    """Load a sorted backfile TSV into the LMDB `docs` sub-DB.

    Args:
        argv: Command-line argument list. First element is the LMDB path; optional
            second element is a path to a pre-sorted TSV file (reads stdin if omitted).

    Returns:
        Exit code (0 on success, 1 on usage error).
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 1:
        print("usage: python -m docdb_id.cli.core <lmdb-path> [<sorted-tsv>]", file=sys.stderr)
        return 1

    lmdb_path = Path(argv[0])
    src_path = Path(argv[1]) if len(argv) > 1 else None

    if src_path is not None:
        with src_path.open("rb") as fh:
            n_docs, n_keys = load_from_tsv(fh, lmdb_path)
    else:
        n_docs, n_keys = load_from_tsv(sys.stdin.buffer, lmdb_path)

    print(f"\n{n_keys:,} unique keys, {n_docs:,} documents -> {lmdb_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
