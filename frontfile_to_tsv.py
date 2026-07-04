#!/usr/bin/env python3
"""Fast DOCDB frontfile XML extractor.

Parses EPO DOCDB XML frontfiles (weekly incremental deliveries) and writes
one TSV part file per input XML. The output is a *changelog* (or "patch"):
every row records one operation to apply to the existing LMDB snapshot, not a
finished record. It is the frontfile counterpart of `backfile_to_tsv.py` and
deliberately performs no LMDB access — mutating the database is the sole job of
`apply_frontfile_to_lmdb.py`, which consumes the sorted output of this script.

Each `<exch:exchange-document>` carries a `status` attribute with one of:

* "A" (amend) and "C" (create): an upsert. The record identified by its full
  `docdb_id` either replaces an existing entry under the same key or is
  appended.
* "D" (delete): a tombstone. The entry with the matching `docdb_id` is removed
  from the key's list (and the key itself dropped once the list empties).

frontfiles must be applied chronologically so that a later modification wins
over an earlier one. A TSV+sort pipeline cannot "process as we go", so each row
carries a sortable `seq` token that encodes the delivery order. Sorting the
combined output on (key, seq) lays every key's full history out in order, and
`apply_frontfile_to_lmdb.py` simply replays it.

TSV columns (one operation per row):

    key              country code + doc-number with leading zeros stripped and
                     upper-cased — identical to the key produced by
                     `backfile_to_tsv.py`, so rows line up with the existing
                     docs sub-DB.
    seq              `<delivery_order><position_in_file>`, fixed-width and
                     zero-padded so a plain LC_ALL=C sort orders operations
                     chronologically. `delivery_order` is the file's index in
                     the sorted input list (weekly frontfiles are date-named,
                     so sorted order is chronological); `position_in_file` is a
                     per-file document counter.
    op               the `status` attribute: "A", "C" or "D".
    docdb_id         country + doc-number + kind, upper-cased.
    orig_doc_number  doc-number from the "original" publication-reference, or
                     empty string. Carried for parity with the backfile TSV
                     (alias tooling); ignored by the docs-DB applier.
    inventor         first inventor (sequence=1, docdb format); tabs -> space.
    date_publ        8-digit publication date.
    family_id        family-id attribute.

Usage:
    frontfile_to_tsv.py [--workers N] --out-dir DIR <path>...

Inputs may be `.xml` / `.xml.gz` files or directories (walked recursively).
"""

from __future__ import annotations

import argparse
import os
import sys
from multiprocessing import Pool
from pathlib import Path

from lxml import etree as LET

from helpers import EntityNormalizingReader, expand_paths, open_xml

# ── XML tag constants ────────────────────────────────────────────────────────

_NS_EXCH = "http://www.epo.org/exchange"
_TAG_DOC = f"{{{_NS_EXCH}}}exchange-document"
_TAG_PUB_REF = f"{{{_NS_EXCH}}}publication-reference"
_TAG_INVENTOR = f"{{{_NS_EXCH}}}inventor"
_TAG_DOCUMENT_ID = "document-id"
_TAG_DOC_NUMBER = "doc-number"
_TAG_NAME = "name"

# frontfile `status` attribute values we know how to apply.
_VALID_STATUS = frozenset({"A", "C", "D"})

# Width of the two halves of the `seq` token. Eight digits cover ~10^8
# deliveries and ten digits cover ~10^10 documents per file; both are far
# beyond any realistic frontfile, and the fixed width makes a lexicographic
# LC_ALL=C sort equivalent to a numeric one.
_SEQ_FILE_WIDTH = 8
_SEQ_POS_WIDTH = 10


def _make_key(cc: bytes, doc_number: bytes) -> bytes:
    """Build an LMDB key by joining a country code and a doc-number with
    leading zeros stripped from the numeric body, upper-cased.

    Identical to the key construction in `backfile_to_tsv.py`, so frontfile
    rows reference exactly the keys created from the backfile snapshot.
    """
    return (cc + doc_number.lstrip(b"0")).upper()


# ── lxml target parser ───────────────────────────────────────────────────────


class _Target:
    """lxml SAX-like target. Allocates no XML tree; one instance per file.

    Mirrors the back-file extractor's target but additionally captures the
    `status` attribute and prefixes each emitted row with a chronological
    `seq` token derived from the file's delivery index and a per-file counter.
    """

    __slots__ = (
        "rows",
        "_file_idx",
        "_pos",
        "_in_doc",
        "_seen_inv",
        "_in_inv",
        "_collecting_inv",
        "_in_pub_original",
        "_in_doc_id",
        "_collecting_orig_docnum",
        "_docdb_id",
        "_orig_docnum",
        "_country",
        "_doc_number",
        "_date_publ",
        "_family_id",
        "_status",
        "_inv_parts",
        "_orig_docnum_parts",
    )

    def __init__(self, file_idx: int) -> None:
        self.rows: list[bytes] = []
        self._file_idx = file_idx
        self._pos = 0
        self._reset()

    def _reset(self) -> None:
        self._in_doc = False
        self._seen_inv = False
        self._in_inv = False
        self._collecting_inv = False
        self._in_pub_original = False
        self._in_doc_id = False
        self._collecting_orig_docnum = False
        self._docdb_id = ""
        self._orig_docnum = ""
        self._country = ""
        self._doc_number = ""
        self._date_publ = ""
        self._family_id = ""
        self._status = ""
        self._inv_parts: list[str] = []
        self._orig_docnum_parts: list[str] = []

    def start(self, tag: str, attrs: dict) -> None:
        if tag == _TAG_DOC:
            self._reset()
            self._in_doc = True
            self._country = attrs.get("country", "")
            self._doc_number = attrs.get("doc-number", "")
            kind = attrs.get("kind", "")
            self._date_publ = attrs.get("date-publ", "")
            self._family_id = attrs.get("family-id", "")
            self._status = attrs.get("status", "").strip()
            if self._country and self._doc_number:
                self._docdb_id = (self._country + self._doc_number + kind).upper()
            return
        if not self._in_doc:
            return

        if tag == _TAG_PUB_REF:
            if attrs.get("data-format") == "original":
                self._in_pub_original = True
            return
        if self._in_pub_original:
            if tag == _TAG_DOCUMENT_ID:
                self._in_doc_id = True
            elif self._in_doc_id and tag == _TAG_DOC_NUMBER and not self._orig_docnum:
                self._collecting_orig_docnum = True
                self._orig_docnum_parts = []
            return

        if self._seen_inv:
            return
        if tag == _TAG_INVENTOR:
            self._in_inv = attrs.get("sequence") == "1" and attrs.get("data-format") == "docdb"
            if self._in_inv:
                self._inv_parts = []
        elif self._in_inv and tag == _TAG_NAME:
            self._collecting_inv = True

    def data(self, text: str) -> None:
        if self._collecting_inv:
            self._inv_parts.append(text.replace("\t", " "))
        elif self._collecting_orig_docnum:
            self._orig_docnum_parts.append(text)

    def end(self, tag: str) -> None:
        if not self._in_doc:
            return
        if self._collecting_orig_docnum and tag == _TAG_DOC_NUMBER:
            self._collecting_orig_docnum = False
            self._orig_docnum = "".join(self._orig_docnum_parts).strip()
            return
        if self._in_doc_id and tag == _TAG_DOCUMENT_ID:
            self._in_doc_id = False
            return
        if self._in_pub_original and tag == _TAG_PUB_REF:
            self._in_pub_original = False
            return
        if self._collecting_inv and tag == _TAG_NAME:
            self._collecting_inv = False
            return
        if self._in_inv and tag == _TAG_INVENTOR:
            self._seen_inv = True
            self._in_inv = False
            return
        if tag == _TAG_DOC:
            self._in_doc = False
            if not self._docdb_id or self._status not in _VALID_STATUS:
                # No usable identifier, or a status we do not know how to apply.
                # Drop it rather than emit an un-appliable row.
                return
            inv = "".join(self._inv_parts).strip()
            seq = f"{self._file_idx:0{_SEQ_FILE_WIDTH}d}{self._pos:0{_SEQ_POS_WIDTH}d}"
            self._pos += 1
            key = _make_key(self._country.encode(), self._doc_number.encode())
            self.rows.append(
                key
                + b"\t"
                + seq.encode()
                + b"\t"
                + self._status.encode()
                + b"\t"
                + self._docdb_id.encode()
                + b"\t"
                + self._orig_docnum.encode()
                + b"\t"
                + inv.encode()
                + b"\t"
                + self._date_publ.encode()
                + b"\t"
                + self._family_id.encode()
                + b"\n"
            )

    def close(self) -> list[bytes]:
        return self.rows


# ── Per-file worker ──────────────────────────────────────────────────────────


def _process_file(job: tuple[int, Path, Path]) -> str | None:
    """Read one frontfile XML, parse, write a part TSV.

    Returns an error string on failure, None on success. Must be a
    module-level function so multiprocessing can pickle it.

    `file_idx` is the file's position in the chronologically sorted input list;
    it becomes the high-order half of every `seq` token in this part, so rows
    from later deliveries sort after rows from earlier ones.
    """
    file_idx, xml_path, out_dir = job
    try:
        target = _Target(file_idx)
        parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
        chunk_size = 1024 * 1024  # 1 MiB
        with open_xml(xml_path) as raw:
            reader = EntityNormalizingReader(raw)
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                parser.feed(chunk)
        rows = parser.close()
        if not rows:
            return None
        out_path = out_dir / f"part_{file_idx:06d}.tsv"
        with out_path.open("wb") as f:
            for row in rows:
                f.write(row)
        return None
    except Exception as exc:
        return f"{xml_path}: {exc}"


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="frontfile XML files, .xml.gz files, or directories (walked recursively).",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for part_NNNNNN.tsv output files (created if absent).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="Worker processes (default: min(8, nproc)). Use 1-2 for SATA.",
    )
    args = ap.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # `expand_paths` walks directories, keeps only .xml/.xml.gz, and returns a
    # sorted list. The index of each path in this list is its delivery order,
    # which we fold into the `seq` token to enforce chronological replay.
    paths = expand_paths(args.inputs)
    if not paths:
        print("no XML files found", file=sys.stderr)
        return 1

    print(
        f"processing {len(paths)} file(s) with {args.workers} worker(s)",
        file=sys.stderr,
    )
    jobs = [(i, p, args.out_dir) for i, p in enumerate(paths)]

    errors = 0
    with Pool(processes=args.workers) as pool:
        for err in pool.imap_unordered(_process_file, jobs, chunksize=1):
            if err:
                print(f"warning: {err}", file=sys.stderr)
                errors += 1

    print(f"done: {len(paths) - errors} ok, {errors} error(s)", file=sys.stderr)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
