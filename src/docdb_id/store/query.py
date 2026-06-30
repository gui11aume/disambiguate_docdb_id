"""Look up DOCDB candidate IDs in an LMDB.

Input lines (TSV):
    <free text>\t"<CC><doc-number><kind>"

For each line we take the quoted candidate ID from the second column, strip the
quotes, drop the trailing kind code (one letter + optional digit), strip any
leading zeros from the doc-number, and query the LMDB `docs` DB.

Lookup is two-tiered:

1. Try the normalised candidate as a key in the `docs` sub-DB. If a record
   list comes back, emit it.
2. Otherwise, try the same key in the `alias` sub-DB. A hit there yields the
   canonical primary key, which we then probe in `docs` to recover the record
   list. This rescues records whose canonical key derives from the office's
   exchange-document number while the input candidate carried the office-native
   original number.

Output lines (TSV):
    <free text>\t<quoted candidate>\t<JSON record list or empty array>
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import IO

import lmdb
import msgpack

from docdb_id.store.schema import ALIAS_DB_NAME, DOCS_DB_NAME

KIND_RE = re.compile(r"[A-Z]\d?$")


def normalize(candidate: str) -> str:
    """Strip quotes, kind code, and leading zeros from the doc-number."""
    s = candidate.strip().strip('"')
    s = KIND_RE.sub("", s)
    if len(s) <= 2:
        return s
    cc, num = s[:2], s[2:]
    return cc + (num.lstrip("0") or "0")


def _fetch_blob(
    txn: lmdb.Transaction,
    docs_db: object,
    alias_db: object | None,
    key: bytes,
) -> bytes | None:
    """Run the two-tier + padding probes and return the raw msgpack blob or None."""
    blob = txn.get(key, db=docs_db)
    if blob is None and alias_db is not None:
        primary = txn.get(key, db=alias_db)
        if primary is not None:
            blob = txn.get(primary, db=docs_db)
    if blob is None and len(key) == 13 and key[6:7] == b"0":
        blob = txn.get(key[:6] + key[7:], db=docs_db)
    if blob is None and len(key) == 11:
        blob = txn.get(key[:6] + b"0" + key[6:], db=docs_db)
    return blob


def lookup_one(
    txn: lmdb.Transaction,
    docs_db: object,
    alias_db: object | None,
    cc: str,
    number: str,
) -> list[dict]:
    """Resolve a single (cc, number) pair against an open LMDB transaction.

    Args:
        txn: Open read transaction.
        docs_db: Handle for the docs sub-DB.
        alias_db: Handle for the alias sub-DB, or None if not present.
        cc: Two-letter country code, e.g. "US".
        number: Doc number without kind code, e.g. "20130143024".

    Returns:
        List of record dicts with keys docdb_id, inventor, date_publ, family_id.
        Empty list when no match is found.
    """
    key = (cc.upper() + number.lstrip("0")).encode()
    blob = _fetch_blob(txn, docs_db, alias_db, key)
    if blob is None:
        return []
    return [
        {"docdb_id": r[0], "inventor": r[1], "date_publ": r[2], "family_id": r[3]}
        for r in msgpack.unpackb(blob, raw=False)
    ]


def run_query(lmdb_path: Path, src: IO[str], out: IO[str]) -> None:
    """Resolve each input line against the LMDB and write JSON record lists to *out*."""
    env = lmdb.open(
        str(lmdb_path),
        readonly=True,
        subdir=lmdb_path.is_dir(),
        lock=False,
        readahead=False,
        max_dbs=3,
    )
    docs_db = env.open_db(DOCS_DB_NAME)
    # Older LMDB envs built before the alias stage existed will not have this
    # sub-DB; treat that as "no aliases known" rather than a fatal error.
    # ReadonlyError is raised (not NotFoundError) when opening a non-existent
    # named DB in a readonly environment, because registering it requires a write txn.
    try:
        alias_db = env.open_db(ALIAS_DB_NAME)
    except (lmdb.NotFoundError, lmdb.ReadonlyError):
        alias_db = None

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
                blob = _fetch_blob(txn, docs_db, alias_db, key)
                records = msgpack.unpackb(blob, raw=False) if blob is not None else []
                print(f"{left}\t{right}\t{json.dumps(records, ensure_ascii=False)}", file=out)
    finally:
        env.close()
