"""Shared parsing and storage helpers for DOCDB back-file and front-file scripts.

Both `build_lmdb_from_backfile.py` (full rebuild from back-files) and
`update_lmdb_from_frontfile.py` (incremental updates from front-files) consume
the same EPO DOCDB XML schema and write into the same LMDB layout, so the
parsing logic, the constants identifying sub-databases and metadata keys, and
the small ISO-timestamp helper all live here.
"""

from __future__ import annotations

import gzip
import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

DEFAULT_MAP_SIZE = 100 * 1024**3  # 100 GiB; LMDB files are sparse.
DEFAULT_COMMIT_EVERY = 100_000

DOCS_DB_NAME = b"docs"
META_DB_NAME = b"meta"
META_KEY_BUILD_STATUS = b"build_status"
META_KEY_LAST_UPDATED = b"last_updated"
BUILD_STATUS_IN_PROGRESS = b"in_progress"
BUILD_STATUS_COMPLETE = b"complete"

NS_EXCHANGE = "http://www.epo.org/exchange"
TAG_EXCHANGE_DOCUMENT = f"{{{NS_EXCHANGE}}}exchange-document"
TAG_INVENTOR = f"{{{NS_EXCHANGE}}}inventor"
TAG_INVENTOR_NAME = f"{{{NS_EXCHANGE}}}inventor-name"
# `<name>` is in no namespace (the root only declares `xmlns:exch=…`,
# not a default namespace), so its tag is just `name`.
TAG_NAME = "name"

XML_FILE_SUFFIXES = (".xml", ".xml.gz")
GZIP_MAGIC = b"\x1f\x8b"

# Front-file `status` attribute values on `<exch:exchange-document>`.
STATUS_AMEND = "A"
STATUS_DELETE = "D"
STATUS_CREATE = "C"

# Type alias for a single document record.
Record = list[str]


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_xml(path: Path) -> IO[bytes]:
    """Open `path` as a binary stream, transparently decompressing gzip content."""
    with path.open("rb") as fh:
        magic = fh.read(len(GZIP_MAGIC))
    if magic == GZIP_MAGIC:
        return gzip.open(path, "rb")
    return path.open("rb")


def is_xml_file(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in XML_FILE_SUFFIXES)


def expand_paths(paths: Iterable[Path]) -> list[Path]:
    """Resolve input arguments to a flat list of XML files.

    Directories are walked recursively and only files whose name ends with
    one of `XML_FILE_SUFFIXES` are kept. Non-existent paths are warned
    about and skipped.
    """
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            for sub in sorted(path.rglob("*")):
                if sub.is_file() and is_xml_file(sub):
                    files.append(sub)
        elif path.is_file():
            files.append(path)
        else:
            logger.warning(f"input path does not exist, skipping: {path}")
    return files


def first_inventor_name(doc_elem: ET.Element) -> str:
    """Return the name of the first `docdb`-format inventor, or `""`.

    The first inventor is the `<exch:inventor>` child of
    `<exch:inventors>` with `sequence="1"` and `data-format="docdb"`.
    Other formats (e.g. `epodoc`) and other sequences are ignored.
    """
    for inventor in doc_elem.iter(TAG_INVENTOR):
        if inventor.get("sequence") != "1":
            continue
        if inventor.get("data-format") != "docdb":
            continue
        name_node = inventor.find(f"{TAG_INVENTOR_NAME}/{TAG_NAME}")
        if name_node is not None and name_node.text:
            return name_node.text.strip()
        return ""
    return ""


def parse_document(doc_elem: ET.Element) -> tuple[str, Record, str] | None:
    """Extract `(key, [docdb_id, first_inventor, publication_date], status)` from a document element.

    `status` is the value of the `status` attribute on the
    `<exch:exchange-document>` element (only present in front-files, where
    it is one of `"A"`, `"D"` or `"C"`). It is the empty string when the
    attribute is missing, which is the normal case for back-files.
    """
    country = (doc_elem.get("country") or "").strip()
    doc_number = (doc_elem.get("doc-number") or "").strip()
    if not country or not doc_number:
        return None
    kind = (doc_elem.get("kind") or "").strip()
    date_publ = (doc_elem.get("date-publ") or "").strip()
    status = (doc_elem.get("status") or "").strip()
    key = f"{country}{doc_number}"
    docdb_id = f"{country}{doc_number}{kind}"
    return key, [docdb_id, first_inventor_name(doc_elem), date_publ], status


def iter_documents_in_file(xml_path: Path) -> Iterator[tuple[str, Record, str]]:
    """Stream `(key, record, status)` triples from a single XML file.

    Uses `iterparse` and clears each processed `<exch:exchange-document>`
    element (plus all of its now-cleared siblings hanging off the root) so
    that memory usage stays flat regardless of file size.
    """
    with open_xml(xml_path) as fh:
        context = iter(ET.iterparse(fh, events=("start", "end")))
        try:
            _, root = next(context)
        except StopIteration:
            return
        for event, elem in context:
            if event != "end" or elem.tag != TAG_EXCHANGE_DOCUMENT:
                continue
            parsed = parse_document(elem)
            if parsed is not None:
                yield parsed
            elem.clear()
            # Drop processed siblings from the root so memory stays flat.
            del root[:]


def iter_all_documents(xml_paths: list[Path]) -> Iterator[tuple[str, Record, str]]:
    """Stream `(key, record, status)` triples from every input XML file."""
    for path in xml_paths:
        logger.info(f"processing {path}")
        try:
            yield from iter_documents_in_file(path)
        except ET.ParseError as exc:
            logger.error(f"XML parse error in {path}: {exc}; continuing with next file")
            continue
