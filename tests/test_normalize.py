"""Tests for normalize.py — entity normalization and streaming reader."""

from __future__ import annotations

import io

from docdb_id.normalize import (
    EntityNormalizingReader,
    normalize_alternate_identifier,
)

# ── normalize_alternate_identifier ────────────────────────────────────────────────────


def test_normalize_alternate_identifier_strips_leading_zeros_w_cc():
    assert normalize_alternate_identifier("US0000123456") == b"US123456"

def test_normalize_alternate_identifier_strips_leading_zeros_wo_cc():
    # XX is not a valid country code, so the input is returned unchanged.
    assert normalize_alternate_identifier("XX0000123456") == b"XX0000123456"
    # Without any country code, leading zeros are stripped.
    assert normalize_alternate_identifier("0000123456") == b"123456"

def test_normalize_alternate_identifier_strips_whitespace():
    assert normalize_alternate_identifier("US 0123456") == b"US123456"


def test_normalize_alternate_identifier_strips_separators():
    assert normalize_alternate_identifier("US-0123456") == b"US123456"


def test_normalize_alternate_identifier_upper_cases():
    assert normalize_alternate_identifier("us123456") == b"US123456"


def test_normalize_alternate_identifier_strips_trailing_kind_if_decimal_suffix():
    # "." followed by digits at end is stripped.
    assert normalize_alternate_identifier("US123456.1") == b"US123456"


def test_normalize_alternate_identifier_no_valid_cc_returns_cleaned_unchanged():
    assert normalize_alternate_identifier("888888-1") == b"8888881"
    assert normalize_alternate_identifier("  1234 / A1  ") == b"1234A1"


def test_normalize_alternate_identifier_dot_not_trailing_version_is_kept():
    # Removes digits but not something else, like "R1".
    assert normalize_alternate_identifier("US8000000.R1") == b"US8000000.R1"


def test_normalize_alternate_identifier_multiple_dots_strips_only_last_version():
    # Not an identifier, but this is how the rule works.
    assert normalize_alternate_identifier("US1.2.3") == b"US1.2"


def test_normalize_alternate_identifier_hyphen_and_slash_are_stripped():
    assert normalize_alternate_identifier("US-2000123456-A1") == b"US2000123456A1"
    assert normalize_alternate_identifier("US 2000/123456 A1") == b"US2000123456A1"


# ── EntityNormalizingReader._entity_safe_prefix_len ──────────────────────────

def _reader(data: bytes) -> EntityNormalizingReader:
    return EntityNormalizingReader(io.BytesIO(data))


def test_safe_prefix_no_amp_returns_full_length():
    r = _reader(b"")
    data = b"hello world"
    assert r._entity_safe_prefix_len(data) == len(data)


def test_safe_prefix_complete_entity_returns_full_length():
    r = _reader(b"")
    data = b"&amp;"
    assert r._entity_safe_prefix_len(data) == len(data)


def test_safe_prefix_partial_entity_at_end_cuts_before_amp():
    r = _reader(b"")
    data = b"text&EAC"
    result = r._entity_safe_prefix_len(data)
    assert result == 4  # cut before '&'


# ── EntityNormalizingReader.read ─────────────────────────────────────────────


def test_reader_passthrough_plain_text():
    r = _reader(b"<doc>hello</doc>")
    assert r.read(1024) == b"<doc>hello</doc>"


def test_reader_rewrites_entity():
    r = _reader(b"<n>&EACUTE;</n>")
    assert r.read(1024) == b"<n></n>"


def test_reader_handles_entity_split_across_chunks():
    # "&EACUTE;" split as b"&EAC" then b"UTE;"
    data = b"<n>&EACUTE;</n>"
    r = EntityNormalizingReader(io.BytesIO(data))
    chunk1 = r.read(6)   # reads up to the safe boundary before '&'
    chunk2 = r.read(1024)
    combined = chunk1 + chunk2
    assert combined == b"<n></n>"


def test_reader_read_all_negative():
    r = _reader(b"&lt;x&gt;")
    assert r.read(-1) == b"&lt;x&gt;"


def test_reader_empty_input():
    r = _reader(b"")
    assert r.read(1024) == b""
