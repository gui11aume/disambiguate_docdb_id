"""Look up DOCDB candidate IDs in an LMDB.

Pipe TSV (`<free text> <TAB> "<candidate-id>"`) on stdin, or pass an input
file as the second argument.

Usage:
    python -m docdb_id.cli.query <lmdb> [<input>]      # input defaults to stdin
"""

from __future__ import annotations

import sys
from pathlib import Path

from docdb_id.store.query import run_query


def main(argv: list[str] | None = None) -> int:
    """Look up DOCDB candidate IDs in an LMDB.

    Args:
        argv: Command-line argument list. First element is the LMDB path; optional
            second element is an input file path (reads stdin if omitted).

    Returns:
        Exit code (0 on success, 1 on usage error).
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 1:
        print("usage: python -m docdb_id.cli.query <lmdb> [<input>]", file=sys.stderr)
        return 1

    lmdb_path = Path(argv[0])
    if len(argv) > 1:
        with open(argv[1]) as src:
            run_query(lmdb_path, src, sys.stdout)
    else:
        run_query(lmdb_path, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
