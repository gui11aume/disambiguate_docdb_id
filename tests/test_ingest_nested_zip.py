"""Tests for streaming nested-zip expansion (one inner zip at a time)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from docdb_id.bdds.ingest import for_each_nested_xml


def _write_inner_zip(path: Path, xml_name: str, xml_body: bytes) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(xml_name, xml_body)


def _write_outer_zip(path: Path, inners: list[tuple[str, str, bytes]]) -> None:
    """Build an outer delivery zip whose members are themselves zip files.

    Args:
        path: Destination outer zip path.
        inners: List of (inner_zip_name, xml_name, xml_body).
    """
    with zipfile.ZipFile(path, "w") as outer:
        for inner_name, xml_name, xml_body in inners:
            buf = Path(path.parent / f"_tmp_{inner_name}")
            _write_inner_zip(buf, xml_name, xml_body)
            outer.write(buf, arcname=f"Root/DOC/{inner_name}")
            buf.unlink()


def test_for_each_nested_xml_processes_one_at_a_time(tmp_path: Path):
    """At most one XML (and no leftover inner zips) may sit under work_dir
    while the callback runs; after the walk, work_dir must be empty.
    """
    outer = tmp_path / "delivery.zip"
    work = tmp_path / "work"
    work.mkdir()
    _write_outer_zip(
        outer,
        [
            ("a.zip", "DOCDB-a.xml", b"<a/>"),
            ("b.zip", "DOCDB-b.xml", b"<b/>"),
            ("c.zip", "DOCDB-c.xml", b"<c/>"),
        ],
    )

    seen: list[str] = []
    peaks: list[tuple[int, int]] = []

    def handle(xml_path: Path) -> None:
        seen.append(xml_path.name)
        xmls = list(work.glob("*.xml"))
        zips = list(work.glob("*.zip"))
        peaks.append((len(xmls), len(zips)))
        assert xml_path.exists()
        assert xml_path.read_bytes() in {b"<a/>", b"<b/>", b"<c/>"}

    n_xml, errors = for_each_nested_xml(outer, work, handle)

    assert errors == []
    assert n_xml == 3
    assert seen == ["DOCDB-a.xml", "DOCDB-b.xml", "DOCDB-c.xml"]
    assert peaks == [(1, 1), (1, 1), (1, 1)]
    assert list(work.iterdir()) == []


def test_for_each_nested_xml_deletes_before_next(tmp_path: Path):
    """The previous XML must be gone by the time the next callback fires."""
    outer = tmp_path / "delivery.zip"
    work = tmp_path / "work"
    work.mkdir()
    _write_outer_zip(
        outer,
        [
            ("a.zip", "DOCDB-a.xml", b"<a/>"),
            ("b.zip", "DOCDB-b.xml", b"<b/>"),
        ],
    )

    previous: Path | None = None

    def handle(xml_path: Path) -> None:
        nonlocal previous
        if previous is not None:
            assert not previous.exists()
        previous = xml_path

    n_xml, errors = for_each_nested_xml(outer, work, handle)
    assert errors == []
    assert n_xml == 2
    assert previous is not None and not previous.exists()
