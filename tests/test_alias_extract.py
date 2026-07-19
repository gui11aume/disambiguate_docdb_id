"""Tests for shared alias derivation helpers."""

from __future__ import annotations

from docdb_id.alias.extract import (
    aliases_for_document,
    alt_alias,
    key_synonyms,
)


def test_key_synonyms_zero_stripped():
    key = b"US2013014302"
    aliases = set(key_synonyms(key))
    assert b"US201314302" in aliases


def test_alt_alias_skips_when_equal_to_key():
    key = b"US1234567"
    outcome = alt_alias(key, b"1234567")
    assert outcome.alias is None
    assert outcome.skipped_equal is True


def test_alt_alias_from_office_number():
    key = b"US8888888"
    alt = b"888888-1"
    outcome = alt_alias(key, alt)
    assert outcome.alias == b"US8888881"


def test_aliases_for_document_combines_key_and_alt():
    key = b"US2013014302"
    alt = b"888888-1"
    aliases = set(aliases_for_document(key, alt))
    assert b"US201314302" in aliases
    assert b"US8888881" in aliases


def test_aliases_for_document_empty_alt_still_has_key_synonyms():
    key = b"US2013014302"
    aliases = list(aliases_for_document(key, b""))
    assert b"US201314302" in aliases
