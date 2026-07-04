#!/usr/bin/env python3
"""Query an LMDB built by ``construct_lmdb.py`` using docdbids from a JSONL file.

For each line in the input JSONL file, the value of the ``docdbid`` field is
used as a key into the LMDB. The stored value (a msgpack-encoded list of
``[pub_no, name]`` pairs) is decoded and printed.

The docdbid is expected to be in the same form as the LMDB keys produced by
``construct_lmdb.py`` (country code + number, no dashes, no kind code, e.g.
``FI20225602``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import lmdb
import msgpack

logger = logging.getLogger("query_lmdb")

DOCDBID_FIELD = "docdbid"


def iter_docdbids(jsonl_path: Path, field: str) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_no, docdbid_value)`` for every JSONL record that has the field.

    Lines that are blank, fail to parse as JSON, or are missing the field are
    logged as warnings and skipped (line counter still advances).
    """
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("line %d: invalid JSON (%s); skipping", line_no, exc)
                continue
            if not isinstance(record, dict):
                logger.warning("line %d: JSON value is not an object; skipping", line_no)
                continue
            if field not in record:
                logger.warning("line %d: missing %r field; skipping", line_no, field)
                continue
            yield line_no, record[field]


def lookup(txn: lmdb.Transaction, docdbid: Any) -> list[Any] | None:
    """Look up a docdbid in the LMDB and return the decoded value or ``None``."""
    key_bytes = str(docdbid).encode("utf-8")
    raw = txn.get(key_bytes)
    if raw is None:
        return None
    return msgpack.unpackb(raw, raw=False)


def query(
    lmdb_path: Path,
    jsonl_path: Path,
    *,
    field: str,
    limit: int | None,
) -> tuple[int, int]:
    """Run the lookup loop. Returns ``(n_found, n_missing)``."""
    env = lmdb.open(
        str(lmdb_path),
        readonly=True,
        subdir=lmdb_path.is_dir(),
        lock=False,
        readahead=False,
    )

    n_found = 0
    n_missing = 0
    try:
        with env.begin(write=False, buffers=False) as txn:
            for line_no, docdbid in iter_docdbids(jsonl_path, field):
                value = lookup(txn, docdbid)
                if value is None:
                    print(f"{docdbid}\t(missing)")
                    n_missing += 1
                else:
                    print(f"{docdbid}\t{value}")
                    n_found += 1
                if limit is not None and (n_found + n_missing) >= limit:
                    logger.info("reached --limit=%d, stopping", limit)
                    break
                _ = line_no
    finally:
        env.close()
    return n_found, n_missing


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "lmdb",
        type=Path,
        help="Path to the LMDB environment created by construct_lmdb.py.",
    )
    parser.add_argument(
        "jsonl",
        type=Path,
        help="Input JSONL file. Each line must be a JSON object with a docdbid field.",
    )
    parser.add_argument(
        "--field",
        default=DOCDBID_FIELD,
        help=f"Name of the JSON field holding the docdbid (default: {DOCDBID_FIELD!r}).",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Stop after this many lookups (default: process the whole file).",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.lmdb.exists():
        logger.error("LMDB path does not exist: %s", args.lmdb)
        return 1
    if not args.jsonl.exists():
        logger.error("JSONL input file does not exist: %s", args.jsonl)
        return 1

    n_found, n_missing = query(
        args.lmdb,
        args.jsonl,
        field=args.field,
        limit=args.limit,
    )
    logger.info("done: %d found, %d missing", n_found, n_missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
