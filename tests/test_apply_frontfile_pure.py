"""Tests for store/apply_frontfile.py — pure record manipulation functions.

These are unit tests that require no LMDB environment: `upsert_record` and
`remove_record` operate on plain Python lists and are exercised in isolation
so correctness bugs surface without integration overhead.
"""

from __future__ import annotations

from docdb_id.store.apply_frontfile import remove_record, upsert_record

# Record type is list[str], with fields [docdb_id, inventor, date_publ, family_id].
R1 = ["US8000000A1", "SMITH", "20050101", "10"]
R2 = ["US8000000B2", "KREITER", "20100531", "123"]
R3 = ["EP1234567A1", "JONES", "20000101", "5"]


# ── upsert_record ──────────────────────────────────────────────────────────────


def test_upsert_replaces_matching_docdb_id():
    """When an existing entry has the same docdb_id, the entry is replaced in-place.

    Key property: `upsert_record` mutates `existing` and returns it,
    so the caller's reference sees the updated record.
    """
    existing = [R1]
    upsert_record(existing, ["US8000000A1", "NEWINV", "20200101", "99"])
    assert existing[0] == ["US8000000A1", "NEWINV", "20200101", "99"]


def test_upsert_replaces_second_of_two_entries():
    """Replacement must work for any position in the list, not just index 0."""
    existing = [R1, R2]
    upsert_record(existing, ["US8000000B2", "REPLACEMENT", "19700101", "0"])
    assert existing[1] == ["US8000000B2", "REPLACEMENT", "19700101", "0"]
    assert existing[0] == R1  # untouched


def test_upsert_appends_when_no_match():
    """When no entry in the existing list shares the docdb_id, the record is appended."""
    existing = [R1]
    upsert_record(existing, R3)
    assert len(existing) == 2
    assert existing[1] == R3


def test_upsert_on_empty_list_appends():
    """Upserting into an empty list is equivalent to initial creation."""
    existing: list[list[str]] = []
    result = upsert_record(existing, R1)
    assert result is existing
    assert existing == [R1]


def test_upsert_skips_empty_entries_in_list():
    """Entries that are empty (falsy) in the list should be skipped during
    matching, then the new record is appended. This guards against a corrupted
    record list where a prior delete left a placeholder.
    """
    existing: list[list[str]] = [R1, [], R3]  # type: ignore[list-item]
    upsert_record(existing, R2)
    assert existing == [R1, [], R3, R2]


# ── remove_record ─────────────────────────────────────────────────────────────


def test_remove_record_removes_matching_entry():
    """The entry with the matching docdb_id is removed from the list."""
    existing = [R1, R2, R3]
    result = remove_record(existing, "US8000000B2")
    assert result == [R1, R3]


def test_remove_record_returns_same_list_when_no_match():
    """When no entry matches, the list is returned unchanged (new list object)."""
    existing = [R1, R3]
    result = remove_record(existing, "US8000000B2")
    assert result == existing  # same elements


def test_remove_record_on_empty_list_returns_empty_list():
    """Removing from an empty list should be a no-op."""
    existing: list[list[str]] = []
    result = remove_record(existing, "US8000000A1")
    assert result == []


def test_remove_record_skips_empty_entries_but_keeps_them():
    """Empty (falsy) entries in the list are preserved but cannot match,
    since `not (entry and entry[0] == docdb_id)` skips them.
    """
    existing: list[list[str]] = [R1, [], R3]  # type: ignore[list-item]
    result = remove_record(existing, "US8000000B2")  # no match
    assert result == [R1, [], R3]
