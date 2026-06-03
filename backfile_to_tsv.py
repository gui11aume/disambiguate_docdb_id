#!/usr/bin/env python3
"""Fast DOCDB XML extractor.

Parses EPO DOCDB XML back-files and writes one sorted TSV part file per input
XML, ready for the docdb-tools/merge + load_lmdb_from_tsv.py pipeline.

Usage:
    build_lmdb_with_backfile.py [--workers N] --out-dir DIR <path>...

--workers (default: min(32, nproc)) controls process-level parallelism.
For a single cold SATA disk, --workers 1 or 2 is often fastest since
multiple processes reading simultaneously thrash the drive's seek queue.
Raise for SSD/NVMe or when input and output live on separate disks.

Output: one part_NNNNNN.tsv per XML file in <out-dir>.

TSV columns:
    key              the lookup key: country code concatenated with the
                     `doc-number` attribute from <exch:exchange-document>,
                     with leading zeros stripped from the numeric body.
    docdb_id         built directly from the attributes of
                        <exch:exchange-document country="..." doc-number="..." kind="...">
                     as `country + doc-number + kind` (e.g. "AM170U",
                     "US20130143024A1", "CA571119"). Taking the identifier
                     from the very first informative line of the record
                     avoids relying on any particular <publication-reference>
                     child being present.
    orig_doc_number  the doc-number text from
                        <exch:publication-reference data-format="original">
                                <document-id><doc-number>...</doc-number></document-id>
                     when the record carries an "original" reference;
                     empty string otherwise. The tab separator is emitted
                     either way so the column stays positional.
    inventor         first inventor (sequence=1, docdb format); tabs → space
    date_publ        8-digit publication date from <exch:exchange-document>
    family_id        family-id attribute from <exch:exchange-document>
"""

from __future__ import annotations

import argparse
import os
import sys
from multiprocessing import Pool
from pathlib import Path

from lxml import etree as LET

# ── XML tag constants ────────────────────────────────────────────────────────

_NS_EXCH = "http://www.epo.org/exchange"
_TAG_DOC = f"{{{_NS_EXCH}}}exchange-document"
_TAG_PUB_REF = f"{{{_NS_EXCH}}}publication-reference"
_TAG_INVENTOR = f"{{{_NS_EXCH}}}inventor"
_TAG_DOCUMENT_ID = "document-id"
_TAG_DOC_NUMBER = "doc-number"
_TAG_NAME = "name"


def _make_key(cc: bytes, doc_number: bytes) -> bytes:
    """Build an LMDB key by joining a country code and a doc-number with
    leading zeros stripped from the numeric body. Both inputs are
    expected to be already-trimmed bytes (e.g. taken straight from
    <exch:exchange-document country="..." doc-number="...">).

    The result is upper-cased so keys are case-insensitive for callers
    (some DOCDB records carry lower-case kind suffixes in the
    doc-number, which would otherwise produce duplicate keys)."""
    return (cc + doc_number.lstrip(b"0")).upper()


# ── lxml target parser ───────────────────────────────────────────────────────


class _Target:
    """lxml SAX-like target.  Allocates no XML tree; one instance per file.

    Uses __slots__ for fast attribute access across millions of callbacks.
    The docdb_id is assembled from the `country`, `doc-number` and `kind`
    attributes of the `<exch:exchange-document>` element itself — the very
    first informative line of the record — so we never depend on any
    particular `<publication-reference>` child appearing in the document.
    """

    __slots__ = (
        "rows",
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
        "_inv_parts",
        "_orig_docnum_parts",
    )

    def __init__(self) -> None:
        self.rows: list[bytes] = []
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
            # Build the docdb_id from the very first informative line of
            # the record. Upper-casing matches the convention used by the
            # epodoc form (e.g. "AM170U", "US20130143024A1") and keeps
            # keys/ids case-insensitive for downstream consumers.
            if self._country and self._doc_number:
                self._docdb_id = (self._country + self._doc_number + kind).upper()
            return
        if not self._in_doc:
            return

        # Capture doc-number text from the "original" publication-reference
        # so downstream consumers can see the publishing office's native
        # number alongside the docdb_id assembled from the document
        # attributes above.
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

        # Only capture the first inventor with sequence="1" (DOCDB convention).
        # If absent, inventor column stays empty.
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
            # Replace tabs as they arrive (cheaper than post-processing)
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
            if self._docdb_id:
                inv = "".join(self._inv_parts).strip()
                # The key comes from `_make_key(country, doc-number)` and the
                # docdb_id from `country + doc-number + kind` — both taken
                # straight off the `<exch:exchange-document>` attributes.
                # The "original" publication-reference, when present, is
                # emitted in its own column; `_orig_docnum` defaults to ""
                # so an absent original reference still yields a positional
                # tab.
                key = _make_key(self._country.encode(), self._doc_number.encode())
                self.rows.append(
                    key
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
            self._in_doc = False

    def close(self) -> list[bytes]:
        return self.rows


# ── Per-file worker ──────────────────────────────────────────────────────────


def _process_file(job: tuple[int, Path, Path]) -> str | None:
    """Read one XML file, parse, write a part TSV.

    Returns an error string on failure, None on success. Must be a
    module-level function so multiprocessing can pickle it.
    """
    idx, xml_path, out_dir = job
    try:
        target = _Target()
        parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
        chunk_size = 1024 * 1024  # 1 MiB
        with xml_path.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                parser.feed(chunk)
        rows = parser.close()
        if not rows:
            return None
        # Write rows incrementally to avoid b"".join() peak memory
        out_path = out_dir / f"part_{idx:06d}.tsv"
        with out_path.open("wb") as f:
            for row in rows:
                f.write(row)
        return None
    except Exception as exc:
        return f"{xml_path}: {exc}"


# ── Path collection ──────────────────────────────────────────────────────────


def _collect_paths(inputs: list[Path]) -> list[Path]:
    result: list[Path] = []
    for p in inputs:
        if p.is_dir():
            for sub in sorted(p.rglob("*")):
                n = sub.name.lower()
                if sub.is_file() and n.endswith(".xml"):
                    result.append(sub)
        elif p.is_file():
            result.append(p)
    return result


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
        help="XML files or directories (walked recursively).",
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
        help="Worker processes (default: min(32, nproc)). Use 1-2 for SATA.",
    )
    args = ap.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    paths = _collect_paths(args.inputs)
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
