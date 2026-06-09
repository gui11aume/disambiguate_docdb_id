#!/usr/bin/env python3
"""Apply a sorted frontfile changelog TSV to an existing LMDB.

This is the only step in the frontfile path that mutates the database; the
extraction (`frontfile_to_tsv.py`) and the global ordering (`sort`) produce a
plain TSV with no LMDB access, keeping parsing and storage cleanly separated.

Input is the changelog produced by `frontfile_to_tsv.py`, already sorted with
`LC_ALL=C sort -t$'\\t' -k1,1 -k2,2` so that rows are grouped by key and, within
each key, ordered chronologically by the `seq` token. Each line has eight
tab-separated columns:

    key \\t seq \\t op \\t docdb_id \\t orig_doc_number \\t inventor \\t date_publ \\t family_id

For every key we read the existing record list once, replay that key's
operations in order, and write the result back:

* "A" (amend) / "C" (create): upsert — replace the entry whose `docdb_id`
  matches, otherwise append. Because operations are replayed in `seq` order, a
  later amend overwrites an earlier one.
* "D" (delete): remove the entry whose `docdb_id` matches. When the list
  becomes empty the LMDB key itself is deleted.

The `orig_doc_number` column is carried for parity with the backfile TSV (alias
tooling) but is not stored in the docs record, matching `initialize_core_from_tsv.py`.

The target LMDB must already be in the `complete` state (left by
`initialize_core_from_tsv.py` or a previous successful apply). While the update
runs, `build_status` is flipped to `in_progress` and only set back to `complete`
after the final commit, so a mid-run crash leaves a database that readers
recognize as incomplete.

Usage:
    apply_frontfile_to_lmdb.py <lmdb-path> [<sorted-changelog.tsv>]   # else stdin
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import lmdb
import msgpack

from helpers import (
    BUILD_STATUS_COMPLETE,
    BUILD_STATUS_IN_PROGRESS,
    DEFAULT_COMMIT_EVERY,
    DEFAULT_MAP_SIZE,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_BUILD_STATUS,
    META_KEY_LAST_UPDATED,
    STATUS_AMEND,
    STATUS_CREATE,
    STATUS_DELETE,
    Record,
    now_iso,
)

logger = logging.getLogger("apply_frontfile_to_lmdb")


def upsert_record(existing: list[Record], record: Record) -> list[Record]:
    """Replace the entry whose `docdb_id` matches `record[0]`, appending otherwise."""
    docdb_id = record[0]
    for i, entry in enumerate(existing):
        if entry and entry[0] == docdb_id:
            existing[i] = record
            return existing
    existing.append(record)
    return existing


def remove_record(existing: list[Record], docdb_id: str) -> list[Record]:
    """Return `existing` with any entry matching `docdb_id` removed."""
    return [entry for entry in existing if not (entry and entry[0] == docdb_id)]


class ApplyStats:
    """Counters returned by `apply_changelog`."""

    __slots__ = ("amended", "created", "deleted", "skipped", "key_deletions", "keys_touched")

    def __init__(self) -> None:
        self.amended = 0
        self.created = 0
        self.deleted = 0
        self.skipped = 0
        self.key_deletions = 0
        self.keys_touched = 0

    @property
    def total_applied(self) -> int:
        return self.amended + self.created + self.deleted


def apply_changelog(
    src,
    lmdb_path: Path,
    *,
    map_size: int = DEFAULT_MAP_SIZE,
    commit_every: int = DEFAULT_COMMIT_EVERY,
) -> ApplyStats:
    """Apply the sorted changelog read from *src* to the LMDB at *lmdb_path*."""
    if not lmdb_path.exists():
        raise FileNotFoundError(f"LMDB path does not exist: {lmdb_path}")

    env = lmdb.open(
        str(lmdb_path),
        map_size=map_size,
        subdir=lmdb_path.is_dir(),
        readonly=False,
        meminit=False,
        writemap=True,
        map_async=True,
        sync=False,
        lock=True,
        max_dbs=2,
    )
    docs_db = env.open_db(DOCS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)

    stats = ApplyStats()
    try:
        # Refuse to update a database that is not in the `complete` state:
        # either the core build was interrupted, or a previous apply crashed
        # mid-run. The safe thing is to rebuild from a backfile.
        with env.begin(write=False) as ro_txn:
            existing_status = ro_txn.get(META_KEY_BUILD_STATUS, db=meta_db)
        if existing_status != BUILD_STATUS_COMPLETE:
            raise RuntimeError(
                f"refusing to update LMDB at {lmdb_path}: build_status is "
                f"{existing_status!r}, expected {BUILD_STATUS_COMPLETE!r}. "
                "Rebuild from a backfile."
            )

        with env.begin(write=True) as marker_txn:
            marker_txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_IN_PROGRESS, db=meta_db)
            marker_txn.put(META_KEY_LAST_UPDATED, now_iso().encode("utf-8"), db=meta_db)

        txn = env.begin(write=True)

        def flush(key_bytes: bytes, working: list[Record]) -> None:
            """Persist the accumulated record list for one key (or drop the key)."""
            if working:
                txn.put(key_bytes, msgpack.packb(working, use_bin_type=True), overwrite=True, db=docs_db)
            elif txn.delete(key_bytes, db=docs_db):
                # `delete` returns False when the key was absent (e.g. a delete
                # for a kind that never existed); only count real removals.
                stats.key_deletions += 1

        try:
            current_key: bytes | None = None
            working: list[Record] = []
            keys_since_commit = 0

            for line_no, raw in enumerate(src, start=1):
                line = raw.rstrip(b"\n") if isinstance(raw, bytes) else raw.rstrip("\n")
                if isinstance(line, str):
                    line = line.encode("utf-8")
                if not line:
                    continue
                parts = line.split(b"\t", 7)
                if len(parts) != 8:
                    logger.warning(f"malformed line {line_no}: {line[:80]!r}")
                    stats.skipped += 1
                    continue

                key = parts[0]
                op = parts[2].decode("utf-8")
                docdb_id = parts[3].decode("utf-8")
                # parts[4] is orig_doc_number — carried for alias parity, not stored.
                inventor = parts[5].decode("utf-8")
                date_publ = parts[6].decode("utf-8")
                family_id = parts[7].decode("utf-8")

                if key != current_key:
                    if current_key is not None:
                        flush(current_key, working)
                        stats.keys_touched += 1
                        keys_since_commit += 1
                        if keys_since_commit >= commit_every:
                            txn.commit()
                            txn = env.begin(write=True)
                            keys_since_commit = 0
                    current_key = key
                    existing_raw = txn.get(key, db=docs_db)
                    working = msgpack.unpackb(existing_raw, raw=False) if existing_raw is not None else []

                if op in (STATUS_AMEND, STATUS_CREATE):
                    working = upsert_record(working, [docdb_id, inventor, date_publ, family_id])
                    if op == STATUS_AMEND:
                        stats.amended += 1
                    else:
                        stats.created += 1
                elif op == STATUS_DELETE:
                    working = remove_record(working, docdb_id)
                    stats.deleted += 1
                else:
                    logger.warning(f"unknown op {op!r} on line {line_no} for {docdb_id}; skipping")
                    stats.skipped += 1

            if current_key is not None:
                flush(current_key, working)
                stats.keys_touched += 1

            txn.commit()
        except BaseException:
            txn.abort()
            raise

        with env.begin(write=True) as marker_txn:
            marker_txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
            marker_txn.put(META_KEY_LAST_UPDATED, now_iso().encode("utf-8"), db=meta_db)

        env.sync(True)
    finally:
        env.close()
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
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
        help="Sorted changelog TSV from frontfile_to_tsv.py. Reads stdin if omitted.",
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.changelog is not None:
        with args.changelog.open("rb") as fh:
            stats = apply_changelog(fh, args.lmdb, map_size=args.map_size, commit_every=args.commit_every)
    else:
        stats = apply_changelog(sys.stdin.buffer, args.lmdb, map_size=args.map_size, commit_every=args.commit_every)

    logger.info(
        f"applied {stats.total_applied} operation(s) to {args.lmdb} across "
        f"{stats.keys_touched} key(s): {stats.created} created, {stats.amended} amended, "
        f"{stats.deleted} deleted ({stats.key_deletions} key(s) removed), "
        f"{stats.skipped} skipped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
