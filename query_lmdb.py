#!/usr/bin/env python3
"""Look up DOCDB candidate IDs in an LMDB.

Input lines (TSV):
    <free text>\\t"<CC><doc-number><kind>"

For each line we take the quoted candidate ID from the second column, strip
the quotes, drop the trailing kind code (one letter + optional digit), strip
any leading zeros from the doc-number, and query the LMDB ``docs`` DB.

Lookup is two-tiered:

1. Try the normalised candidate as a key in the ``docs`` sub-DB. If a
   record list comes back, emit it.
2. Otherwise, try the same key in the ``layer_1`` alias sub-DB. A hit
   there yields the canonical primary key, which we then probe in
   ``docs`` to recover the record list. This rescues records whose
   canonical key derives from the office's exchange-document number
   while the input candidate carried the office-native original number
   (e.g. AP/AM/EP-style IDs, or originals with non-standard formatting).

Output lines (TSV):
    <free text>\\t<quoted candidate>\\t<JSON record list or empty array>

Usage:
    query_lmdb.py <lmdb> [<input>]      # input defaults to stdin
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import lmdb
import msgpack

DOCS_DB_NAME = b"docs"
LAYER_1_DB_NAME = b"layer_1"
KIND_RE = re.compile(r"[A-Z]\d?$")


def normalize(candidate: str) -> str:
    """Strip quotes, kind code, and leading zeros from the doc-number."""
    s = candidate.strip().strip('"')
    s = KIND_RE.sub("", s)
    if len(s) <= 2:
        return s
    cc, num = s[:2], s[2:]
    return cc + (num.lstrip("0") or "0")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: query_lmdb.py <lmdb> [<input>]", file=sys.stderr)
        return 1

    lmdb_path = Path(sys.argv[1])
    src = open(sys.argv[2]) if len(sys.argv) > 2 else sys.stdin

    env = lmdb.open(
        str(lmdb_path),
        readonly=True,
        subdir=lmdb_path.is_dir(),
        lock=False,
        readahead=False,
        max_dbs=3,
    )
    docs_db = env.open_db(DOCS_DB_NAME)
    # Older LMDB envs built before the layer_1 stage existed will not
    # have this sub-DB; treat that as "no aliases known" rather than
    # a fatal error.
    try:
        layer_1_db = env.open_db(LAYER_1_DB_NAME)
    except lmdb.NotFoundError:
        layer_1_db = None

    try:
        with env.begin(write=False) as txn:
            for raw in src:
                line = raw.rstrip("\n")
                if "\t" not in line:
                    continue
                left, right = line.split("\t", 1)
                key = normalize(right).encode()
                if not key:
                    continue
                blob = txn.get(key, db=docs_db)
                if blob is None and layer_1_db is not None:
                    primary = txn.get(key, db=layer_1_db)
                    if primary is not None:
                        blob = txn.get(primary, db=docs_db)
                if blob is None and len(key) == 13 and key[6:7] == b"0":
                    blob = txn.get(key[:6] + key[7:], db=docs_db)
                if blob is None and len(key) == 11:
                    blob = txn.get(key[:6] + b"0" + key[6:], db=docs_db)
                records = msgpack.unpackb(blob, raw=False) if blob is not None else []
                print(f"{left}\t{right}\t{json.dumps(records, ensure_ascii=False)}")
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
