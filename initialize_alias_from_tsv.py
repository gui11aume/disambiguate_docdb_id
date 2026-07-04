#!/usr/bin/env python3
"""Load the alias sub-DB from a sorted 2-column TSV.

Input columns:

    processed(orig_doc_number) \\t key

The input must be sorted ascending on column 1 with ``LC_ALL=C`` (which
is what GNU ``sort`` produces by default). Identical lines must already
be deduplicated upstream — pipe through ``sort -u`` between
``extract_alias_tsv.py`` and this loader.

Two correctness invariants are enforced via LMDB primitives instead of
defensive Python code:

* ``append=True`` makes the put fail with ``MDB_KEYEXIST`` if a key
  arrives that is not strictly greater than the previous one. That
  catches mis-sorted input loudly on the first offending row.
* ``overwrite=False`` makes the put fail with ``MDB_KEYEXIST`` if the
  key already exists. Combined with the dedup pass upstream, this
  surfaces only *real* collisions: the same processed alias pointing
  to two different primary keys, which is bad data we want to know
  about.

On either failure the loader aborts the transaction and exits non-zero
after printing both the offending row and the existing mapping (if any),
so the operator can fix the upstream data.

Usage:
    # Typical pipeline:
    extract_alias_tsv.py stage/sorted.tsv | LC_ALL=C sort -u | \\
        initialize_alias_from_tsv.py out/docdb.lmdb

    # Or read from a pre-sorted file:
    initialize_alias_from_tsv.py out/docdb.lmdb stage/alias_sorted.tsv
"""
from __future__ import annotations

import sys
from pathlib import Path

import lmdb

from helpers import (
    ALIAS_DB_NAME,
    BUILD_STATUS_COMPLETE,
    BUILD_STATUS_IN_PROGRESS,
    DEFAULT_COMMIT_EVERY,
    DEFAULT_MAP_SIZE,
    DOCS_DB_NAME,
    META_DB_NAME,
    META_KEY_BUILD_STATUS,
    META_KEY_LAST_UPDATED,
    now_iso,
)

ALIAS_BUILD_STATUS_KEY = b"alias_build_status"
ALIAS_LAST_UPDATED_KEY = b"alias_last_updated"


class _Collision(Exception):
    """Raised by ``_put_or_die`` to short-circuit the load on bad data."""


def _put_or_die(
    txn: lmdb.Transaction,
    cursor: lmdb.Cursor,
    alias_db,
    alias: bytes,
    primary_key: bytes,
    line_no: int,
    last_alias: bytes | None,
) -> None:
    """Append ``(alias, primary_key)`` to the alias DB with loud failures.

    py-lmdb signals ``append=True``/``overwrite=False`` failures by
    returning ``False`` from ``cursor.put`` rather than raising, so we
    have to check the return value explicitly. We then translate it
    into a precise, actionable error message that distinguishes the
    two reasons LMDB rejects the write: a sort violation vs. a
    duplicate alias mapping to a different key.
    """
    if cursor.put(alias, primary_key, append=True, overwrite=False):
        return

    existing = txn.get(alias, db=alias_db)
    if existing is not None:
        if existing == primary_key:
            msg = (
                f"line {line_no}: duplicate row not removed by upstream sort -u "
                f"(alias={alias!r} -> {primary_key!r})"
            )
        else:
            msg = (
                f"line {line_no}: collision on processed alias {alias!r}: "
                f"already maps to {existing!r}, refusing to remap to {primary_key!r}"
            )
    elif last_alias is not None and alias <= last_alias:
        msg = (
            f"line {line_no}: input not sorted ascending: "
            f"alias={alias!r} arrived after {last_alias!r}"
        )
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
    """Load *src* into the ``alias`` sub-DB of an existing LMDB env.

    Returns ``(n_written, n_skipped_docs)``. The env is expected to have
    been built by ``initialize_core_from_tsv.py`` already; this loader
    only adds the alias sub-DB and updates a couple of meta keys.
    """
    if not lmdb_path.exists():
        raise FileNotFoundError(
            f"{lmdb_path} does not exist; build the docs sub-DB first "
            f"with initialize_core_from_tsv.py."
        )

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

    # Idempotent re-runs: clear any previous content of the alias sub-DB
    # before re-loading. The sub-DB itself is preserved so callers don't
    # have to worry about handle invalidation.
    with env.begin(write=True) as txn:
        txn.drop(alias_db, delete=False)
        txn.put(ALIAS_BUILD_STATUS_KEY, BUILD_STATUS_IN_PROGRESS, db=meta_db)
        txn.put(META_KEY_LAST_UPDATED, now_iso().encode(), db=meta_db)

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
            n += 1

            if n % commit_every == 0:
                txn.commit()
                txn = env.begin(write=True)
                cursor = txn.cursor(db=alias_db)
                print(f"\ralias: {n:,} aliases…", end="", file=sys.stderr)

        txn.commit()
    except BaseException:
        txn.abort()
        raise

    with env.begin(write=True) as txn:
        txn.put(ALIAS_BUILD_STATUS_KEY, BUILD_STATUS_COMPLETE, db=meta_db)
        txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
        txn.put(META_KEY_LAST_UPDATED, now_iso().encode(), db=meta_db)

    env.sync(True)
    env.close()
    return n, n_skipped_docs


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: initialize_alias_from_tsv.py <lmdb-path> [<sorted-2col-tsv>]",
            file=sys.stderr,
        )
        return 1

    lmdb_path = Path(sys.argv[1])
    src_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

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
        f"\nalias: {n:,} aliases → {lmdb_path} "
        f"({n_skipped_docs:,} skipped; already in docs)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
