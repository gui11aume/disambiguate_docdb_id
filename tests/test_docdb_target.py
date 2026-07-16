"""Tests for parse/docdb_target.py — key construction and SAX state machines."""

from __future__ import annotations

import io
from lxml import etree as LET

from docdb_id.parse.docdb_target import (
    BackfileTarget,
    FrontfileTarget,
    make_key,
    _SEQ_FILE_WIDTH,
    _SEQ_POS_WIDTH,
)


# ── make_key ────────────────────────────────────────────────────────────────────


def test_make_key_strips_leading_zeros():
    """Regular CC: leading zeros are stripped from the numeric body before upper-casing."""
    assert make_key(b"US", b"00123456") == b"US123456"


def test_make_key_upper_cases_everything():
    """Both CC and doc_number are upper-cased. Lowercase kind suffixes in
    doc_number (e.g. 'b2') appear in some DOCDB records.
    """
    assert make_key(b"us", b"123456b2") == b"US123456B2"


def test_make_key_single_digit_doc_number():
    """Doc_number with a single non-zero digit produces a short key."""
    assert make_key(b"US", b"1") == b"US1"


def test_make_key_all_zeros_becomes_empty_suffix():
    """When doc_number is all zeros, lstrip removes everything.
    The key is just the upper-cased CC.
    """
    assert make_key(b"US", b"00000") == b"US"


def test_make_key_jp_prefix_preserves_digits_after_leading_zeros():
    """JP keys preserve the numeric part as-is (leading zeros are stripped
    by lstrip, but JP special handling for alias normalization happens in
    _normalize_alias, not in make_key).
    """
    assert make_key(b"JP", b"0123456") == b"JP123456"


# ── BackfileTarget (SAX state machine) ─────────────────────────────────────────


_BACKFILE_XML_NS = b'xmlns:exch="http://www.epo.org/exchange"'
_CH_XML_NS = b'xmlns:ch="http://www.epo.org/exchange"'


def _parse_backfile(xml_chunks: list[bytes]) -> list[bytes]:
    target = BackfileTarget()
    parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
    for chunk in xml_chunks:
        parser.feed(chunk)
    result = parser.close()
    if result is None:
        return []
    return result


def test_backfile_single_document_emits_expected_tsv():
    """Feed a minimal exchange-document with all fields and verify the TSV output."""
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<exch:exchange-document country="US" doc-number="8000000" kind="B2"'
        b' date-publ="20110816" family-id="39183031"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'  <ch:publication-reference data-format="original" ' + _CH_XML_NS + b'>'
        b'    <document-id>'
        b'      <doc-number>8,000,000</doc-number>'
        b'    </document-id>'
        b'  </ch:publication-reference>'
        b'  <exch:inventor sequence="1" data-format="docdb">'
        b'    <exch:inventor-name>'
        b'      <name>ROBERT J. GREENBERG</name>'
        b'    </exch:inventor-name>'
        b'  </exch:inventor>'
        b'</exch:exchange-document>'
    )
    rows = _parse_backfile([xml])
    assert rows == [
        b"US8000000\tUS8000000B2\t8,000,000\tROBERT J. GREENBERG\t20110816\t39183031\n"
    ]


def test_backfile_missing_inventor_leaves_column_empty():
    """When no inventor with sequence='1' and data-format='docdb' exists,
    the inventor column is empty.
    """
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<exch:exchange-document country="EP" doc-number="1234567" kind="A1"'
        b' date-publ="20200101" family-id="42"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
    )
    rows = _parse_backfile([xml])
    assert rows == [b"EP1234567\tEP1234567A1\t\t\t20200101\t42\n"]


def test_backfile_missing_country_and_doc_number_skips_document():
    """Documents missing country or doc-number produce no TSV row
    because _docdb_id is empty.
    """
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<exch:exchange-document kind="A1" date-publ="20200101" family-id="42"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
    )
    rows = _parse_backfile([xml])
    assert rows == []


def test_backfile_duplicate_inventors_only_first_sequence_1_captured():
    """When multiple inventors appear, only the first with sequence='1'
    and data-format='docdb' is captured.
    """
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<exch:exchange-document country="US" doc-number="8000000" kind="B2"'
        b' date-publ="20110816" family-id="1"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'  <exch:inventor sequence="1" data-format="docdb">'
        b'    <exch:inventor-name><name>FIRST</name></exch:inventor-name>'
        b'  </exch:inventor>'
        b'  <exch:inventor sequence="2" data-format="docdb">'
        b'    <exch:inventor-name><name>SECOND</name></exch:inventor-name>'
        b'  </exch:inventor>'
        b'</exch:exchange-document>'
    )
    rows = _parse_backfile([xml])
    assert b"FIRST" in rows[0]
    assert b"SECOND" not in rows[0]


def test_backfile_multiple_documents_in_one_stream():
    """Two exchange-documents fed as one contiguous byte-stream (no second
    XML declaration — lxml expects a single logical document) produce two TSV rows.
    """
    xml1_and_2 = (
        b'<?xml version="1.0"?>'
        b'<root>'
        b'<exch:exchange-document country="US" doc-number="8000000" kind="B2"'
        b' date-publ="20110816" family-id="1"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
        b'<exch:exchange-document country="EP" doc-number="1234567" kind="A1"'
        b' date-publ="20200101" family-id="2"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
        b'</root>'
    )
    rows = _parse_backfile([xml1_and_2])
    assert len(rows) == 2


# ── FrontfileTarget (seq token + status filtering) ─────────────────────────────


def _parse_frontfile(xml_chunks: list[bytes], file_idx: int = 0) -> list[bytes]:
    target = FrontfileTarget(file_idx)
    parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
    for chunk in xml_chunks:
        parser.feed(chunk)
    result = parser.close()
    if result is None:
        return []
    return result


def test_frontfile_emits_seq_token_and_op_column():
    """Frontfile rows have 8 columns: key, seq, op, docdb_id, orig, inventor, date, family."""
    xml = (
        b'<?xml version="1.0"?>'
        b'<exch:exchange-document country="US" doc-number="8000000" kind="B2"'
        b' date-publ="20110816" family-id="1" status="C"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
    )
    rows = _parse_frontfile([xml], file_idx=5)
    fields = rows[0].split(b"\t")
    assert len(fields) == 8
    expected_seq = f"{5:0{_SEQ_FILE_WIDTH}d}{0:0{_SEQ_POS_WIDTH}d}".encode()
    assert fields[1] == expected_seq
    assert fields[2] == b"C"


def test_frontfile_drops_documents_with_unknown_status():
    """Only A, C, D are emitted. Status 'X' or '' produces no row."""
    xml = (
        b'<?xml version="1.0"?>'
        b'<exch:exchange-document country="US" doc-number="8000000" kind="B2"'
        b' date-publ="20110816" family-id="1" status="X"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
    )
    rows = _parse_frontfile([xml])
    assert rows == []


def test_frontfile_seq_increments_within_file():
    """Each emitted document increments the per-file position counter.
    Documents are wrapped in a <root> element so lxml treats them as one stream.
    """
    xml_both = (
        b'<?xml version="1.0"?>'
        b'<root>'
        b'<exch:exchange-document country="US" doc-number="8000000" kind="B2"'
        b' date-publ="20110816" family-id="1" status="A"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
        b'<exch:exchange-document country="EP" doc-number="1234567" kind="A1"'
        b' date-publ="20200101" family-id="2" status="C"'
        b' ' + _BACKFILE_XML_NS + b'>'
        b'</exch:exchange-document>'
        b'</root>'
    )
    rows = _parse_frontfile([xml_both])
    pos_0 = f"{0:0{_SEQ_POS_WIDTH}d}".encode()
    pos_1 = f"{1:0{_SEQ_POS_WIDTH}d}".encode()
    assert rows[0].split(b"\t")[1].endswith(pos_0)
    assert rows[1].split(b"\t")[1].endswith(pos_1)
