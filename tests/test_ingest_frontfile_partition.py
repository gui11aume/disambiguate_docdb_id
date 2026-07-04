"""Tests for frontfile ingest resume partitioning."""

from __future__ import annotations

from pathlib import Path

from docdb_id.bdds.ingest import WorkItem, _partition_frontfile_items


def _item(key: str) -> WorkItem:
    return WorkItem(key=key, file_name=f"{key}.zip", local_path=Path(f"/tmp/{key}.zip"))


def test_partition_frontfile_items_splits_on_existing_tsvs(tmp_path: Path):
    out_dir = tmp_path / "parts"
    out_dir.mkdir()
    (out_dir / "part_done_a.tsv").write_text("key\tseq\top\n", encoding="utf-8")
    (out_dir / "part_done_b.tsv").write_text("key\tseq\top\n", encoding="utf-8")

    items = [_item("done_a"), _item("pending"), _item("done_b")]
    pending, already = _partition_frontfile_items(items, out_dir)

    assert [item.key for item in pending] == ["pending"]
    assert already == 2


def test_partition_frontfile_items_skips_applied_parts_without_local_tsv(tmp_path: Path):
    """A part already recorded as applied in the LMDB must be skipped even if
    local staging was wiped and has no matching TSV - this is what makes it
    safe to delete the staging directory between weekly runs."""
    out_dir = tmp_path / "parts"
    out_dir.mkdir()

    items = [_item("already_applied"), _item("pending")]
    pending, already = _partition_frontfile_items(items, out_dir, frozenset({"already_applied"}))

    assert [item.key for item in pending] == ["pending"]
    assert already == 1
