"""Load the `alias` sub-DB from a sorted 2-column TSV.

Input columns:

    processed(orig_doc_number) \t key

The input must be sorted ascending on column 1 with `LC_ALL=C` (which is what
GNU `sort` produces by default) and contain exactly one row per alias.
Upstream (see the Makefile's alias stage) `docdb_id.alias.extract` emits a
third `date_publ` column, the stream is sorted on `(alias, date_publ)`,
collapsed to the first row per alias (oldest publication wins on a collision),
and the date column is stripped before it reaches this loader.

Two correctness invariants are enforced via LMDB primitives instead of defensive
Python code:

* `append=True` makes the put fail if a key arrives that is not strictly
  greater than the previous one. That catches mis-sorted input loudly on the
  first offending row.
* `overwrite=False` makes the put fail if the key already exists. Since the
  upstream collapse keeps exactly one row per alias, a duplicate key here means
  the collapse pass was skipped or produced mis-ordered output.

On either failure the loader aborts the transaction and raises `_Collision`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import lmdb

from docdb_id.store.schema import (
    ALIAS_DB_NAME,
    BUILD_STATUS_COMPLETE,
    BUILD_STATUS_IN_PROGRESS,
    DEFAULT_COMMIT_EVERY,
    DEFAULT_MAP_SIZE,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_ALIAS_BUILD_STATUS,
    META_KEY_ALIAS_LAST_UPDATED,
    now_iso,
)


class _Collision(Exception):
    """Raised by `_put_or_die` to short-circuit the load on bad data."""


def add_alias(txn: lmdb.Transaction, alias_db, docs_db, alias: bytes, key: bytes) -> None:
    """Insert *alias* -> *key* unless *alias* is already a docs key or taken."""
    if txn.get(alias, db=docs_db) is not None:
        return
    txn.put(alias, key, overwrite=False, db=alias_db)


def remove_alias(txn: lmdb.Transaction, alias_db, alias: bytes, key: bytes) -> None:
    """Delete *alias* only when it still maps to *key*."""
    if txn.get(alias, db=alias_db) == key:
        txn.delete(alias, db=alias_db)


def _put_or_die(
    txn: lmdb.Transaction,
    cursor: lmdb.Cursor,
    alias_db,
    alias: bytes,
    primary_key: bytes,
    line_no: int,
    last_alias: bytes | None,
) -> None:
    """Append `(alias, primary_key)` to the alias DB with loud failures.

    py-lmdb signals `append=True`/`overwrite=False` failures by returning
    `False` from `cursor.put` rather than raising, so we check the return
    value explicitly and translate it into a precise, actionable error that
    distinguishes a sort violation from a duplicate alias mapping.
    """
    if cursor.put(alias, primary_key, append=True, overwrite=False):
        return

    existing = txn.get(alias, db=alias_db)
    if existing is not None:
        if existing == primary_key:
            msg = f"line {line_no}: duplicate row not removed by upstream sort -u (alias={alias!r} -> {primary_key!r})"
        else:
            msg = (
                f"line {line_no}: collision on processed alias {alias!r}: "
                f"already maps to {existing!r}, refusing to remap to {primary_key!r}"
            )
    elif last_alias is not None and alias <= last_alias:
        msg = f"line {line_no}: input not sorted ascending: alias={alias!r} arrived after {last_alias!r}"
    else:
        msg = f"line {line_no}: lmdb rejected put for alias={alias!r} -> {primary_key!r}"

    raise _Collision(msg)


def load_alias(
    src,
    lmdb_path: Path,
    *,
    map_size: int = DEFAULT_MAP_SIZE,
    commit_every: int = DEFAULT_COMMIT_EVERY,
) -> tuple[int, int]:
    """Load *src* into the `alias` sub-DB of an existing LMDB env.

    Returns `(n_written, n_skipped_docs)`. The env is expected to have been
    built by `docdb_id.store.core` already; this loader only adds the alias
    sub-DB and updates a couple of meta keys.
    """
    if not lmdb_path.exists():
        raise FileNotFoundError(f"{lmdb_path} does not exist; build the docs sub-DB first with docdb_id.store.core.")

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

    alias_db = env.open_db(ALIAS_DB_NAME)
    docs_db = env.open_db(DOCS_DB_NAME, create=False)
    meta_db = env.open_db(META_DB_NAME)

    # Idempotent re-runs: clear any previous content of the alias sub-DB before
    # re-loading. The sub-DB itself is preserved so callers don't have to worry
    # about handle invalidation.
    with env.begin(write=True) as txn:
        txn.drop(alias_db, delete=False)
        txn.put(META_KEY_ALIAS_BUILD_STATUS, BUILD_STATUS_IN_PROGRESS, db=meta_db)
        txn.put(META_KEY_ALIAS_LAST_UPDATED, now_iso().encode(), db=meta_db)

    n = 0
    n_skipped_docs = 0
    last_alias: bytes | None = None

    txn = env.begin(write=True)
    cursor = txn.cursor(db=alias_db)
    try:
        for line_no, raw in enumerate(src, start=1):
            line = raw.rstrip(b"\n") if isinstance(raw, bytes) else raw.rstrip("\n").encode()
            if not line:
                continue
            parts = line.split(b"\t", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise _Collision(f"line {line_no}: malformed row: {line[:120]!r}")

            alias, primary_key = parts
            if len(alias) >= 3 and alias[2:3] == b"0" and alias[:2] != b"JP":
                raise _Collision(
                    f"line {line_no}: alias {alias!r} has '0' at position 2; "
                    f"only JP aliases are allowed to keep that form"
                )
            if txn.get(alias, db=docs_db) is not None:
                n_skipped_docs += 1
                continue

            _put_or_die(txn, cursor, alias_db, alias, primary_key, line_no, last_alias)
            last_alias = alias

            if n % commit_every == 0:
                txn.commit()
                txn = env.begin(write=True)
                cursor = txn.cursor(db=alias_db)
                print(f"\ralias: {n:,} aliases...", end="", file=sys.stderr)

        txn.commit()
    except BaseException:
        txn.abort()
        raise

    with env.begin(write=True) as txn:
        txn.put(META_KEY_ALIAS_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
        txn.put(META_KEY_ALIAS_LAST_UPDATED, now_iso().encode(), db=meta_db)

    env.sync(True)
    env.close()
    return n, n_skipped_docs
