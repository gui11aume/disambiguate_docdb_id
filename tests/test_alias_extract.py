"""Tests for shared alias derivation helpers."""

from __future__ import annotations

from docdb_id.alias.extract import aliases_for_document, key_synonyms, orig_aliases


def test_key_synonyms_zero_stripped():
    key = b"US2013014302"
    aliases = set(key_synonyms(key))
    assert b"US201314302" in aliases


def test_orig_aliases_skips_when_equal_to_key():
    key = b"US1234567"
    batch = orig_aliases(key, b"1234567")
    assert batch.aliases == ()
    assert batch.skipped_equal is True


def test_orig_aliases_from_office_number():
    key = b"US8888888"
    orig = b"888888-1"
    batch = orig_aliases(key, orig)
    assert batch.aliases == (b"US8888881",)


def test_aliases_for_document_combines_key_and_orig():
    key = b"US2013014302"
    orig = b"888888-1"
    aliases = set(aliases_for_document(key, orig))
    assert b"US201314302" in aliases
    assert b"US8888881" in aliases


def test_aliases_for_document_empty_orig_still_has_key_synonyms():
    key = b"US2013014302"
    aliases = list(aliases_for_document(key, b""))
    assert b"US201314302" in aliases
