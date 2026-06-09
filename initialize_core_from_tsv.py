#!/usr/bin/env python3
"""Load a sorted TSV stream into LMDB.

Reads from stdin (or a file passed as the second argument).  The input must
already be sorted by the first column with LC_ALL=C, which is exactly what
GNU sort produces.  Each line has six tab-separated columns:

    key \\t docdb_id \\t original_doc_number \\t first_inventor_name \\t publication_date \\t family_id

The `original_doc_number` column (3rd) is present in the TSV but is not
stored in the LMDB record; it is read and discarded. Tab/newline/CR in the
inventor field are replaced with a space at extraction time; all other
fields are plain ASCII. No backslash-unescaping is needed.

Usage:
    # Typical pipeline:
    sort -t $'\\t' -k1,1 ... stage/raw.tsv | uv run python load_lmdb_from_tsv.py out/docdb.lmdb

    # Or read from a pre-sorted file:
    uv run python load_lmdb_from_tsv.py out/docdb.lmdb stage/sorted.tsv
"""

from __future__ import annotations

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
    now_iso,
)


def load_from_tsv(
    src,
    lmdb_path: Path,
    *,
    map_size: int = DEFAULT_MAP_SIZE,
    commit_every: int = DEFAULT_COMMIT_EVERY,
) -> tuple[int, int]:
    """Read sorted TSV from *src* and write to a new LMDB at *lmdb_path*.

    Returns (n_docs, n_keys).
    """
    if lmdb_path.exists():
        import shutil

        if lmdb_path.is_dir():
            shutil.rmtree(lmdb_path)
        else:
            lmdb_path.unlink()

    lmdb_path.mkdir(parents=True, exist_ok=True)

    env = lmdb.open(
        str(lmdb_path),
        map_size=map_size,
        subdir=True,
        readonly=False,
        meminit=False,
        writemap=True,
        map_async=True,
        sync=False,
        lock=True,
        max_dbs=3,
    )

    docs_db = env.open_db(DOCS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)

    with env.begin(write=True) as txn:
        txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_IN_PROGRESS, db=meta_db)
        txn.put(META_KEY_LAST_UPDATED, now_iso().encode(), db=meta_db)

    n_keys = 0
    n_docs = 0
    current_key: bytes | None = None
    current_records: list[list[str]] = []
    current_ids: set[str] = set()

    txn = env.begin(write=True)
    try:
        cursor = txn.cursor(db=docs_db)

        def flush() -> None:
            payload = msgpack.packb(current_records, use_bin_type=True)
            cursor.put(current_key, payload, append=True)

        for line_no, raw in enumerate(src, start=1):
            line = raw.rstrip(b"\n") if isinstance(raw, bytes) else raw.rstrip("\n")
            if isinstance(line, str):
                line = line.encode("utf-8")
            parts = line.split(b"\t", 5)
            if len(parts) != 6:
                print(f"warning: malformed line {line_no}: {line[:80]!r}", file=sys.stderr)
                continue

            key = parts[0]
            if len(key) >= 3 and key[2:3] == b"0" and key[:2] != b"JP":
                raise ValueError(
                    f"line {line_no}: key {key!r} has '0' at position 2; only JP keys are allowed to keep that form"
                )
            docdb_id = parts[1].decode("utf-8")
            # parts[2] is the original_doc_number alias from the backfile
            # extractor. Intentionally ignored: only the canonical docdb_id
            # is stored in the LMDB record.
            inventor = parts[3].decode("utf-8")
            date_publ = parts[4].decode("utf-8")
            family_id = parts[5].decode("utf-8")

            if key != current_key:
                if current_key is not None:
                    flush()
                    n_keys += 1
                    if n_keys % commit_every == 0:
                        txn.commit()
                        txn = env.begin(write=True)
                        cursor = txn.cursor(db=docs_db)
                        print(f"\r{n_keys:,} keys, {n_docs:,} docs…", end="", file=sys.stderr)
                current_key = key
                current_records = []
                current_ids = set()

            if docdb_id not in current_ids:
                current_records.append([docdb_id, inventor, date_publ, family_id])
                current_ids.add(docdb_id)
                n_docs += 1

        if current_key is not None:
            flush()
            n_keys += 1

        txn.commit()
    except BaseException:
        txn.abort()
        raise

    with env.begin(write=True) as txn:
        txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
        txn.put(META_KEY_LAST_UPDATED, now_iso().encode(), db=meta_db)

    env.sync(True)

    with env.begin(write=False) as ro:
        n_keys = ro.stat(docs_db)["entries"]

    env.close()
    return n_docs, n_keys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: load_lmdb_from_tsv.py <lmdb-path> [<sorted-tsv>]", file=sys.stderr)
        return 1

    lmdb_path = Path(sys.argv[1])
    src_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if src_path is not None:
        with src_path.open("rb") as fh:
            n_docs, n_keys = load_from_tsv(fh, lmdb_path)
    else:
        n_docs, n_keys = load_from_tsv(sys.stdin.buffer, lmdb_path)

    print(f"\n{n_keys:,} unique keys, {n_docs:,} documents → {lmdb_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
