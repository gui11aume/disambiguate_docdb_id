"""Tests for normalize.py — entity normalization and streaming reader."""

from __future__ import annotations

import io

from docdb_id.normalize import (
    EntityNormalizingReader,
    _entity_safe_prefix_len,
    normalize_xml_entities,
    processed_doc_number,
)


# ── processed_doc_number ────────────────────────────────────────────────────


def test_processed_doc_number_strips_leading_zeros():
    assert processed_doc_number("US0000123456") == b"US123456"


def test_processed_doc_number_strips_whitespace():
    assert processed_doc_number("US 0123456") == b"US123456"


def test_processed_doc_number_strips_separators():
    assert processed_doc_number("US-0123456") == b"US123456"


def test_processed_doc_number_upper_cases():
    assert processed_doc_number("us123456") == b"US123456"


def test_processed_doc_number_strips_trailing_kind_if_decimal_suffix():
    # "." followed by digits only at end is stripped
    assert processed_doc_number("US123456.1") == b"US123456"


def test_processed_doc_number_too_short_returns_empty():
    assert processed_doc_number("US") == b"US"


# ── normalize_xml_entities ──────────────────────────────────────────────────


def test_normalize_known_entity():
    assert normalize_xml_entities(b"&EACUTE;") == b"&#201;"


def test_normalize_builtin_entity_unchanged():
    assert normalize_xml_entities(b"&amp;") == b"&amp;"
    assert normalize_xml_entities(b"&lt;") == b"&lt;"


def test_normalize_unknown_entity_escaped():
    result = normalize_xml_entities(b"&UnknownXYZ;")
    assert b"&amp;UnknownXYZ;" == result


def test_normalize_no_entities_unchanged():
    data = b"<doc>hello world</doc>"
    assert normalize_xml_entities(data) == data


# ── _entity_safe_prefix_len ──────────────────────────────────────────────────


def test_safe_prefix_no_amp_returns_full_length():
    data = b"hello world"
    assert _entity_safe_prefix_len(data) == len(data)


def test_safe_prefix_complete_entity_returns_full_length():
    data = b"&amp;"
    assert _entity_safe_prefix_len(data) == len(data)


def test_safe_prefix_partial_entity_at_end_cuts_before_amp():
    data = b"text&EAC"
    result = _entity_safe_prefix_len(data)
    assert result == 4  # cut before '&'


# ── EntityNormalizingReader ──────────────────────────────────────────────────


def _reader(data: bytes) -> EntityNormalizingReader:
    return EntityNormalizingReader(io.BytesIO(data))


def test_reader_passthrough_plain_text():
    r = _reader(b"<doc>hello</doc>")
    assert r.read(1024) == b"<doc>hello</doc>"


def test_reader_rewrites_entity():
    r = _reader(b"<n>&EACUTE;</n>")
    assert r.read(1024) == b"<n>&#201;</n>"


def test_reader_handles_entity_split_across_chunks():
    # "&EACUTE;" split as b"&EAC" then b"UTE;"
    data = b"<n>&EACUTE;</n>"
    r = EntityNormalizingReader(io.BytesIO(data))
    chunk1 = r.read(6)   # reads up to the safe boundary before '&'
    chunk2 = r.read(1024)
    combined = chunk1 + chunk2
    assert combined == b"<n>&#201;</n>"


def test_reader_read_all_negative():
    r = _reader(b"&lt;x&gt;")
    assert r.read(-1) == b"&lt;x&gt;"


def test_reader_empty_input():
    r = _reader(b"")
    assert r.read(1024) == b""
