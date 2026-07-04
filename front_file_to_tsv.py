#!/usr/bin/env python3
"""Apply EPO DOCDB front-file XML updates to an existing LMDB.

Front-files describe incremental changes to the back-file snapshot. Every
`<exch:exchange-document>` carries a `status` attribute on the document
element with one of three values:

* "A" (amend) and "C" (create) are plain replacements: the record
  identified by `<country><doc-number><kind>` is inserted into the list
  stored at the LMDB key `<country><doc-number>`, replacing any existing
  entry that shares the same full `docdb_id`. If no such entry exists
  yet, the new one is appended.

* "D" (delete) removes the entry with the matching `docdb_id` from the
  list. When the list becomes empty as a result, the LMDB key itself is
  deleted.

The target LMDB must already exist and must have been left in the
`complete` state by `build_lmdb_from_backfile.py` (or by a previous
successful run of this script). Refuse to update databases whose
`build_status` is not `complete`. While the update runs, `build_status`
is flipped to `in_progress`; it is set back to `complete` only after the
last document has been applied and the final commit has succeeded. A
mid-run crash therefore leaves a database that readers will recognize as
incomplete.

Multiple input paths can be passed on the command line; directories are
walked recursively for `*.xml` and `*.xml.gz` files. Gzipped files are
transparently decompressed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import lmdb
import msgpack
from tqdm import tqdm

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
    expand_paths,
    iter_all_documents,
    now_iso,
)

logger = logging.getLogger("update_lmdb_from_frontfile")


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


class UpdateStats:
    """Counters returned by `update_lmdb`."""

    __slots__ = ("amended", "created", "deleted", "skipped", "key_deletions")

    def __init__(self) -> None:
        self.amended = 0
        self.created = 0
        self.deleted = 0
        self.skipped = 0
        self.key_deletions = 0

    @property
    def total_applied(self) -> int:
        return self.amended + self.created + self.deleted


def update_lmdb(
    xml_paths: list[Path],
    lmdb_path: Path,
    *,
    map_size: int,
    commit_every: int,
) -> UpdateStats:
    """Apply front-file updates from `xml_paths` to the LMDB at `lmdb_path`."""
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

    stats = UpdateStats()
    try:
        # Refuse to update a database that is not in the `complete` state:
        # either the back-file build was interrupted, or a previous update
        # crashed mid-run. In either case the safe thing to do is to ask the
        # operator to rebuild from scratch.
        with env.begin(write=False) as ro_txn:
            existing_status = ro_txn.get(META_KEY_BUILD_STATUS, db=meta_db)
        if existing_status != BUILD_STATUS_COMPLETE:
            raise RuntimeError(
                f"refusing to update LMDB at {lmdb_path}: build_status is "
                f"{existing_status!r}, expected {BUILD_STATUS_COMPLETE!r}. "
                "Rebuild from a back-file."
            )

        # Flip the marker to in-progress before applying any change.
        with env.begin(write=True) as marker_txn:
            marker_txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_IN_PROGRESS, db=meta_db)
            marker_txn.put(META_KEY_LAST_UPDATED, now_iso().encode("utf-8"), db=meta_db)

        seen_in_txn = 0
        txn = env.begin(write=True)
        try:
            for key, record, status in tqdm(iter_all_documents(xml_paths), unit="doc", desc="updating LMDB"):
                key_bytes = key.encode("utf-8")
                docdb_id = record[0]

                if status in (STATUS_AMEND, STATUS_CREATE):
                    existing_raw = txn.get(key_bytes, db=docs_db)
                    existing = msgpack.unpackb(existing_raw, raw=False) if existing_raw is not None else []
                    updated = upsert_record(existing, record)
                    txn.put(key_bytes, msgpack.packb(updated, use_bin_type=True), overwrite=True, db=docs_db)
                    if status == STATUS_AMEND:
                        stats.amended += 1
                    else:
                        stats.created += 1
                elif status == STATUS_DELETE:
                    existing_raw = txn.get(key_bytes, db=docs_db)
                    if existing_raw is None:
                        # Nothing to delete; common when the front-file targets
                        # a kind code that was never in the back-file.
                        stats.deleted += 1
                    else:
                        existing = msgpack.unpackb(existing_raw, raw=False)
                        updated = remove_record(existing, docdb_id)
                        if updated:
                            txn.put(key_bytes, msgpack.packb(updated, use_bin_type=True), overwrite=True, db=docs_db)
                        else:
                            txn.delete(key_bytes, db=docs_db)
                            stats.key_deletions += 1
                        stats.deleted += 1
                else:
                    logger.warning(f"unknown status {status!r} for {docdb_id}; skipping")
                    stats.skipped += 1
                    continue

                seen_in_txn += 1
                if seen_in_txn >= commit_every:
                    txn.commit()
                    txn = env.begin(write=True)
                    seen_in_txn = 0
            txn.commit()
        except BaseException:
            txn.abort()
            raise

        # Mark the database complete again. Until this transaction commits,
        # the on-disk status remains `in_progress`.
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
        "inputs",
        type=Path,
        nargs="+",
        help="One or more front-file XML files, .xml.gz files, or directories (walked recursively).",
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
        help=f"Commit and reopen the write transaction every N documents (default: {DEFAULT_COMMIT_EVERY}).",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    xml_paths = expand_paths(args.inputs)
    if not xml_paths:
        logger.error(f"no XML files found in inputs: {args.inputs}")
        return 1
    logger.info(f"found {len(xml_paths)} XML file(s) to process")

    stats = update_lmdb(
        xml_paths,
        args.lmdb,
        map_size=args.map_size,
        commit_every=args.commit_every,
    )
    logger.info(
        f"applied {stats.total_applied} update(s) to {args.lmdb}: "
        f"{stats.created} created, {stats.amended} amended, "
        f"{stats.deleted} deleted ({stats.key_deletions} key(s) removed), "
        f"{stats.skipped} skipped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
