#!/usr/bin/env python3
"""Build an LMDB from EPO DOCDB back-file XML files.

For every `<exch:exchange-document>` element found in the input XML files,
one record is added to the LMDB.

* The LMDB key is `<country><doc-number>`, taken from the attributes of
  the `<exch:exchange-document>` element (no dashes, no kind code). For
  example a document with `country="AM"` and `doc-number="170"` is stored
  under the key `AM170`.

* The LMDB value is a list of records. Each record is a triple:

      [docdb_id, first_inventor_name, publication_date]

  where:

  - `docdb_id` is `<country><doc-number><kind>` (e.g. `AM170U`);
  - `first_inventor_name` is the text of
    `<exch:inventor sequence="1" data-format="docdb">/<exch:inventor-name>/<name>`,
    or the empty string when the document has no inventor;
  - `publication_date` is the value of the `date-publ` attribute on
    `<exch:exchange-document>`, kept verbatim as an 8-character
    `YYYYMMDD` string.

Several documents typically share the same `<country><doc-number>` key
because they differ only in their kind code (e.g. `A1`, `C1`). All such
records are merged into a single list under that key. A record is
de-duplicated when its full `docdb_id` (country + number + kind) is
already present in the stored list, so re-running on overlapping inputs
is safe.

Multiple input paths can be passed on the command line. If a path is a
directory, every `*.xml` and `*.xml.gz` file under it (recursively) is
processed. Gzipped files are transparently decompressed.

The LMDB environment contains two named sub-databases:

* `docs` holds the document records described above.
* `meta` holds build metadata as plain byte strings:

      - `build_status`: `in_progress` while a build is running,
        `complete` once it has fully finished. Readers should refuse
        to trust a database whose status is not `complete`.
      - `last_updated`: ISO 8601 UTC timestamp of the most recent
        status write (set both at start and at successful completion).

Values are serialized with msgpack (no extra compression).

Front-file `status` attributes (`A`, `D`, `C`) on input documents are
ignored by this script: back-files are treated as a clean snapshot and
every document is inserted unconditionally. Use
`update_lmdb_from_frontfile.py` to apply incremental updates.
"""

from __future__ import annotations

import argparse
import logging
import shutil
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
    Record,
    expand_paths,
    iter_all_documents,
    now_iso,
)

logger = logging.getLogger("build_lmdb_from_backfile")


def merge_record(existing: list[Record], record: Record) -> list[Record]:
    """Append `record` to `existing` unless its `docdb_id` is already there."""
    docdb_id = record[0]
    for entry in existing:
        if entry and entry[0] == docdb_id:
            return existing
    existing.append(record)
    return existing


def build_lmdb(
    xml_paths: list[Path],
    output_path: Path,
    *,
    map_size: int,
    commit_every: int,
) -> tuple[int, int]:
    """Build the LMDB at `output_path` and return `(n_documents, n_keys)`."""
    if output_path.exists():
        logger.info(f"removing existing output at {output_path}")
        if output_path.is_dir():
            shutil.rmtree(output_path)
        else:
            output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    env = lmdb.open(
        str(output_path),
        map_size=map_size,
        subdir=True,
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

    n_docs = 0
    n_keys = 0
    seen_in_txn = 0
    try:
        # Commit the in-progress marker first so an interruption at any later
        # point leaves a database that readers can recognize as incomplete.
        with env.begin(write=True) as marker_txn:
            marker_txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_IN_PROGRESS, db=meta_db)
            marker_txn.put(META_KEY_LAST_UPDATED, now_iso().encode("utf-8"), db=meta_db)

        txn = env.begin(write=True)
        try:
            for key, record, _status in tqdm(iter_all_documents(xml_paths), unit="doc", desc="writing LMDB"):
                key_bytes = key.encode("utf-8")
                existing_raw = txn.get(key_bytes, db=docs_db)
                if existing_raw is None:
                    payload = msgpack.packb([record], use_bin_type=True)
                    n_keys += 1
                else:
                    existing = msgpack.unpackb(existing_raw, raw=False)
                    merged = merge_record(existing, record)
                    payload = msgpack.packb(merged, use_bin_type=True)
                txn.put(key_bytes, payload, overwrite=True, db=docs_db)
                n_docs += 1
                seen_in_txn += 1
                if seen_in_txn >= commit_every:
                    txn.commit()
                    txn = env.begin(write=True)
                    seen_in_txn = 0
            txn.commit()
        except BaseException:
            txn.abort()
            raise

        # Mark the build complete in its own transaction. If anything before
        # this point fails or crashes, the status stays at `in_progress`.
        with env.begin(write=True) as marker_txn:
            marker_txn.put(META_KEY_BUILD_STATUS, BUILD_STATUS_COMPLETE, db=meta_db)
            marker_txn.put(META_KEY_LAST_UPDATED, now_iso().encode("utf-8"), db=meta_db)

        env.sync(True)  # Force sync to disk (`env` is declared with `sync=False`).
        with env.begin(write=False) as ro_txn:
            n_keys = ro_txn.stat(docs_db)["entries"]
    finally:
        env.close()
    return n_docs, n_keys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "output",
        type=Path,
        help="Path of the LMDB environment to create (wiped and recreated if it exists).",
    )
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="One or more XML files, .xml.gz files, or directories (walked recursively).",
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

    n_docs, n_keys = build_lmdb(
        xml_paths,
        args.output,
        map_size=args.map_size,
        commit_every=args.commit_every,
    )
    logger.info(f"wrote {n_keys} unique key(s) from {n_docs} document(s) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
