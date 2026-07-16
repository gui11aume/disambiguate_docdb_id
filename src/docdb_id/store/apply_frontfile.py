"""Apply a sorted frontfile changelog TSV to an existing LMDB.

This is the only step in the frontfile path that mutates the database; the
extraction and the global ordering (`sort`) produce a plain TSV with no LMDB
access, keeping parsing and storage cleanly separated.

Input is the changelog produced by `docdb_id.parse.docdb_target.FrontfileTarget`,
already sorted with `LC_ALL=C sort -t$'\t' -k1,1 -k2,2` so that rows are
grouped by key and, within each key, ordered chronologically by the `seq`
token. Each line has eight tab-separated columns:

    key \t seq \t op \t docdb_id \t orig_doc_number \t inventor \t date_publ \t family_id

For every key we read the existing record list once, replay that key's
operations in order, and write the result back:

* "A" (amend) / "C" (create): upsert - replace the entry whose `docdb_id`
  matches, otherwise append. Because operations are replayed in `seq` order, a
  later amend overwrites an earlier one.
* "D" (delete): remove the entry whose `docdb_id` matches. When the list
  becomes empty the LMDB key itself is deleted.

The `alias` sub-DB is updated in the same write transaction as `docs`:

* "C" / "A": add every alias derived from `(key, orig_doc_number)` via the
  shared `docdb_id.alias.extract` helpers. On collision the existing mapping is
  kept (assumed older).
* "D": remove orig-derived aliases that still map to this key. Key-derived
  synonyms are removed only when the key is fully deleted from `docs`.

The target LMDB core build must already be in the `complete` state. While the
update runs, `core_build_status` is flipped to `in_progress` and only set back
to `complete` after the final commit, so a mid-run crash leaves a database that
readers recognize as incomplete. When the caller provides frontfile part stems,
they are written to the `meta` sub-DB in the same terminal transaction that
restores the `complete` state, so a partial update is never recorded as
incorporated.
"""

from __future__ import annotations

import logging
from pathlib import Path

import lmdb
import msgpack

from docdb_id.alias.extract import key_synonyms, orig_aliases
from docdb_id.store.alias import add_alias, remove_alias
from docdb_id.store.schema import (
    ALIAS_DB_NAME,
    BUILD_STATUS_COMPLETE,
    BUILD_STATUS_IN_PROGRESS,
    DEFAULT_COMMIT_EVERY,
    DEFAULT_MAP_SIZE,
    DOCS_DB_NAME,
    FRONTFILE_APPLIED_PREFIX,
    META_DB_NAME,
    META_KEY_ALIAS_LAST_UPDATED,
    META_KEY_ALIAS_NO_DANGLING,
    META_KEY_CORE_BUILD_STATUS,
    META_KEY_CORE_LAST_UPDATED,
    META_KEY_FRONTFILE_LAST_APPLIED,
    STATUS_AMEND,
    STATUS_CREATE,
    STATUS_DELETE,
    Record,
    now_iso,
)

logger = logging.getLogger("docdb_id.store.apply_frontfile")


def load_applied_frontfile_parts(lmdb_path: Path) -> frozenset[str]:
    """Return the frontfile part stems already recorded as applied in *lmdb_path*.

    This is the durable record of what has been incorporated: the ingest step
    consults it to skip re-fetching deliveries that were already applied in a
    previous run, even after the local staging directory has been wiped.

    Args:
        lmdb_path: Path to the existing LMDB directory.

    Returns:
        Frozenset of frontfile part stems that have been applied.
    """
    if not lmdb_path.exists():
        return frozenset()
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
            return frozenset()
        applied: set[str] = set()
        with env.begin(write=False) as txn, txn.cursor(db=meta_db) as cursor:
            if cursor.set_range(FRONTFILE_APPLIED_PREFIX):
                for key in cursor.iternext(values=False):
                    if not key.startswith(FRONTFILE_APPLIED_PREFIX):
                        break
                    applied.add(key[len(FRONTFILE_APPLIED_PREFIX) :].decode("utf-8"))
        return frozenset(applied)
    finally:
        env.close()


def upsert_record(existing: list[Record], record: Record) -> list[Record]:
    """Replace the entry whose `docdb_id` matches `record[0]`, appending otherwise.

    Args:
        existing: Current list of records for the key.
        record: New record to upsert.

    Returns:
        Updated list of records.
    """
    docdb_id = record[0]
    for i, entry in enumerate(existing):
        if entry and entry[0] == docdb_id:
            existing[i] = record
            return existing
    existing.append(record)
    return existing


def remove_record(existing: list[Record], docdb_id: str) -> list[Record]:
    """Return `existing` with any entry matching `docdb_id` removed.

    Args:
        existing: Current list of records for the key.
        docdb_id: docdb_id of the entry to remove.

    Returns:
        Filtered list of records.
    """
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
    applied_parts: list[str] | None = None,
    map_size: int = DEFAULT_MAP_SIZE,
    commit_every: int = DEFAULT_COMMIT_EVERY,
) -> ApplyStats:
    """Apply the sorted changelog read from *src* to the LMDB at *lmdb_path*.

    Args:
        src: Iterable of changelog TSV lines (bytes or str), sorted by key
            then seq.
        lmdb_path: Path to the existing LMDB directory.
        applied_parts: Optional list of frontfile part stems to record as
            applied in the terminal meta transaction.
        map_size: Map size for the LMDB environment.
        commit_every: Number of keys to process between commits.

    Returns:
        ApplyStats instance with counters for the operations performed.

    Raises:
        FileNotFoundError: If the LMDB path does not exist.
        RuntimeError: If the core build status is not `complete`.
    """
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
        max_dbs=3,
    )
    docs_db = env.open_db(DOCS_DB_NAME)
    alias_db = env.open_db(ALIAS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)

    stats = ApplyStats()
    try:
        # Refuse to update a database that is not in the `complete` state: either
        # the core build was interrupted, or a previous apply crashed mid-run.
        # The safe thing is to rebuild from a backfile.
        with env.begin(write=False) as ro_txn:
            existing_status = ro_txn.get(META_KEY_CORE_BUILD_STATUS, db=meta_db)
        if existing_status != BUILD_STATUS_COMPLETE:
            raise RuntimeError(
                f"refusing to update LMDB at {lmdb_path}: core_build_status is "
                f"{existing_status!r}, expected {BUILD_STATUS_COMPLETE!r}. "
                "Rebuild from a backfile."
            )

        with env.begin(write=True) as marker_txn:
            marker_txn.put(META_KEY_CORE_BUILD_STATUS, BUILD_STATUS_IN_PROGRESS, db=meta_db)
            marker_txn.put(META_KEY_CORE_LAST_UPDATED, now_iso().encode("utf-8"), db=meta_db)

        txn = env.begin(write=True)

        def flush(key_bytes: bytes, working: list[Record]) -> None:
            """Persist the accumulated record list for one key (or drop the key).

            Args:
                key_bytes: Binary key for the current record group.
                working: List of records to persist. If empty, the key is
                    deleted from the docs sub-DB.
            """
            if working:
                txn.put(key_bytes, msgpack.packb(working, use_bin_type=True), overwrite=True, db=docs_db)
            elif txn.delete(key_bytes, db=docs_db):
                # `delete` returns False when the key was absent (e.g. a delete
                # for a kind that never existed); only count real removals.
                stats.key_deletions += 1
                for alias in key_synonyms(key_bytes):
                    remove_alias(txn, alias_db, alias, key_bytes)

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
                orig = parts[4]
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
                    for alias in key_synonyms(key):
                        add_alias(txn, alias_db, docs_db, alias, key)
                    for alias in orig_aliases(key, orig).aliases:
                        add_alias(txn, alias_db, docs_db, alias, key)
                    if op == STATUS_AMEND:
                        stats.amended += 1
                    else:
                        stats.created += 1
                elif op == STATUS_DELETE:
                    working = remove_record(working, docdb_id)
                    for alias in orig_aliases(key, orig).aliases:
                        remove_alias(txn, alias_db, alias, key)
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
            applied_at = now_iso().encode("utf-8")
            for part in sorted(set(applied_parts or [])):
                marker_txn.put(FRONTFILE_APPLIED_PREFIX + part.encode("utf-8"), applied_at, db=meta_db)
            if applied_parts:
                marker_txn.put(META_KEY_FRONTFILE_LAST_APPLIED, applied_at, db=meta_db)
            marker_txn.put(META_KEY_CORE_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
            marker_txn.put(META_KEY_CORE_LAST_UPDATED, applied_at, db=meta_db)
            marker_txn.put(META_KEY_ALIAS_LAST_UPDATED, applied_at, db=meta_db)
            # A frontfile apply can leave aliases pointing at deleted keys, so the
            # alias DB is no longer known to be free of dangling entries. Drop the
            # assertion; `docdb_id.store.alias.prune_orphan_aliases` re-establishes it.
            marker_txn.delete(META_KEY_ALIAS_NO_DANGLING, db=meta_db)

        env.sync(True)
    finally:
        env.close()
    return stats
