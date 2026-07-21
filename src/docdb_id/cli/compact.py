"""Compact an LMDB environment to its dense on-disk size.

After a cold backfile load the data.mdb is typically a ~100 GiB map. Compacting
collapses it to the real payload (~12 GiB) so it can be uploaded to a small VPS.
Run this on the build host; do not compact on the VPS (needs a full second copy).

Usage:
    python -m docdb_id.cli.compact <lmdb-path>              # in place
    python -m docdb_id.cli.compact <lmdb-path> <dest-path>  # copy to dest
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from docdb_id.store.compact import compact_in_place, compact_lmdb

logger = logging.getLogger("docdb_id.cli.compact")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the compact command."""
    parser = argparse.ArgumentParser(
        prog="docdb_id.cli.compact",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("lmdb", type=Path, help="Source LMDB environment directory.")
    parser.add_argument(
        "dest",
        type=Path,
        nargs="?",
        help="Optional destination directory. If omitted, compact in place.",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Compact an LMDB environment.

    Args:
        argv: Command-line argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 on success, 1 on error).
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        if args.dest is None:
            compact_in_place(args.lmdb)
        else:
            compact_lmdb(args.lmdb, args.dest)
            logger.info(f"compacted {args.lmdb} → {args.dest}")
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
