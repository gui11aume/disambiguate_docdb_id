"""Apply a sorted frontfile changelog TSV to an existing LMDB.

Input is the changelog produced by `docdb_id.cli.frontfile`, already sorted
with `LC_ALL=C sort -t$'\t' -k1,1 -k2,2` so rows are grouped by key and, within
each key, ordered chronologically by the `seq` token.

Usage:
    python -m docdb_id.cli.apply_frontfile <lmdb-path> <sorted-changelog.tsv> [<part-tsv>...]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from docdb_id.store.apply_frontfile import apply_changelog
from docdb_id.store.schema import DEFAULT_COMMIT_EVERY, DEFAULT_MAP_SIZE

logger = logging.getLogger("docdb_id.cli.apply_frontfile")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="docdb_id.cli.apply_frontfile",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "lmdb",
        type=Path,
        help="Path of the existing LMDB environment to update in place.",
    )
    parser.add_argument(
        "changelog",
        type=Path,
        nargs="?",
        help="Sorted changelog TSV from docdb_id.cli.frontfile. Reads stdin if omitted.",
    )
    parser.add_argument(
        "part_tsvs",
        type=Path,
        nargs="*",
        help="Frontfile part TSVs included in the sorted changelog; recorded in LMDB metadata.",
    )
    parser.add_argument(
        "--map-size",
        type=int,
        default=DEFAULT_MAP_SIZE,
        help=f"LMDB map_size in bytes (default: {DEFAULT_MAP_SIZE}). Files are sparse, this is only an upper bound.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=DEFAULT_COMMIT_EVERY,
        help=f"Commit and reopen the write transaction every N keys (default: {DEFAULT_COMMIT_EVERY}).",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    return parser.parse_args(argv)


def part_stem(path: Path) -> str:
    """Return the stable frontfile part identity for a generated part TSV path."""
    stem = path.stem
    if stem.startswith("part_"):
        return stem[len("part_") :]
    return stem


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    applied_parts = [part_stem(path) for path in args.part_tsvs]
    try:
        if args.changelog is not None:
            with args.changelog.open("rb") as fh:
                stats = apply_changelog(
                    fh,
                    args.lmdb,
                    applied_parts=applied_parts,
                    map_size=args.map_size,
                    commit_every=args.commit_every,
                )
        else:
            stats = apply_changelog(
                sys.stdin.buffer,
                args.lmdb,
                applied_parts=applied_parts,
                map_size=args.map_size,
                commit_every=args.commit_every,
            )
    except RuntimeError as exc:
        logger.error("%s", exc)
        logger.error(
            "To recover: rebuild the LMDB from the backfile using docdb_id.cli.backfile, "
            "then re-apply any frontfile parts."
        )
        return 1

    logger.info(
        f"applied {stats.total_applied} operation(s) to {args.lmdb} across "
        f"{stats.keys_touched} key(s): {stats.created} created, {stats.amended} amended, "
        f"{stats.deleted} deleted ({stats.key_deletions} key(s) removed), "
        f"{stats.skipped} skipped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
