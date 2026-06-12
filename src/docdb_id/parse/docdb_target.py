"""Consolidated lxml target parsers for DOCDB exchange documents.

Both the backfile (full snapshot) and the frontfile (weekly incremental
changelog) read the same EPO DOCDB XML schema and extract the same fields from
each `<exch:exchange-document>`. The only differences are:

* the frontfile additionally captures the `status` attribute (A/C/D) and
  prefixes every row with a chronological `seq` token;
* the emitted TSV row layouts differ (6 columns vs. 8).

`_DocdbTargetBase` implements the shared SAX-style state machine (no XML tree is
built; one instance per file). `BackfileTarget` and `FrontfileTarget` only
differ in how they turn the accumulated per-document state into a TSV row.
"""

from __future__ import annotations

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

# Width of the two halves of the frontfile `seq` token. Eight digits cover
# ~10^8 deliveries and ten digits cover ~10^10 documents per file; both are far
# beyond any realistic frontfile, and the fixed width makes a lexicographic
# LC_ALL=C sort equivalent to a numeric one.
_SEQ_FILE_WIDTH = 8
_SEQ_POS_WIDTH = 10


def make_key(cc: bytes, doc_number: bytes) -> bytes:
    """Build an LMDB key by joining a country code and a doc-number.

    Leading zeros are stripped from the numeric body and the result is
    upper-cased so keys are case-insensitive for callers (some DOCDB records
    carry lower-case kind suffixes in the doc-number, which would otherwise
    produce duplicate keys). Both inputs are expected to be already-trimmed
    bytes taken straight from `<exch:exchange-document country=... doc-number=...>`.
    """
    return (cc + doc_number.lstrip(b"0")).upper()


class _DocdbTargetBase:
    """Shared lxml target state machine. Allocates no XML tree.

    Uses `__slots__` for fast attribute access across millions of callbacks.
    The `docdb_id` is assembled from the `country`, `doc-number` and
    `kind` attributes of the `<exch:exchange-document>` element itself - the
    very first informative line of the record - so we never depend on any
    particular `<publication-reference>` child appearing in the document.

    Subclasses implement `_emit_document` to append a TSV row to `self.rows`.
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
        "_status",
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
            # Build the docdb_id from the very first informative line of the
            # record. Upper-casing matches the epodoc convention (e.g. "AM170U",
            # "US20130143024A1") and keeps keys/ids case-insensitive downstream.
            if self._country and self._doc_number:
                self._docdb_id = (self._country + self._doc_number + kind).upper()
            return
        if not self._in_doc:
            return

        # Capture doc-number text from the "original" publication-reference so
        # downstream consumers can see the publishing office's native number
        # alongside the docdb_id assembled from the document attributes above.
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
        # If absent, the inventor column stays empty.
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
            # Replace tabs as they arrive (cheaper than post-processing).
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
            self._emit_document()

    def _emit_document(self) -> None:
        raise NotImplementedError

    def close(self) -> list[bytes]:
        return self.rows


class BackfileTarget(_DocdbTargetBase):
    """Extract full snapshot records into 6-column TSV rows.

    Columns: `key, docdb_id, orig_doc_number, inventor, date_publ, family_id`.
    """

    __slots__ = ()

    def _emit_document(self) -> None:
        if not self._docdb_id:
            return
        inv = "".join(self._inv_parts).strip()
        key = make_key(self._country.encode(), self._doc_number.encode())
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


class FrontfileTarget(_DocdbTargetBase):
    """Extract changelog operations into 8-column TSV rows.

    Columns: `key, seq, op, docdb_id, orig_doc_number, inventor, date_publ,
    family_id`. Each row is prefixed with a `seq` token whose high half is the
    file's chronological delivery index (`file_idx`) and whose low half is a
    per-file document counter, so a plain `LC_ALL=C` sort orders operations
    chronologically. Rows with no usable identifier or an unknown `status` are
    dropped rather than emitted as un-appliable operations.
    """

    __slots__ = ("_file_idx", "_pos")

    def __init__(self, file_idx: int) -> None:
        self._file_idx = file_idx
        self._pos = 0
        super().__init__()

    def _emit_document(self) -> None:
        if not self._docdb_id or self._status not in _VALID_STATUS:
            return
        inv = "".join(self._inv_parts).strip()
        seq = f"{self._file_idx:0{_SEQ_FILE_WIDTH}d}{self._pos:0{_SEQ_POS_WIDTH}d}"
        self._pos += 1
        key = make_key(self._country.encode(), self._doc_number.encode())
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
