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
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from html.entities import name2codepoint
from pathlib import Path
from typing import IO

from lxml import etree as LET

from country_codes import VALID_CC

logger = logging.getLogger(__name__)

DEFAULT_MAP_SIZE = 100 * 1024**3  # 100 GiB; LMDB files are sparse.
DEFAULT_COMMIT_EVERY = 100_000

DOCS_DB_NAME = b"docs"
ALIAS_DB_NAME = b"alias"
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
XML_BUILTIN_ENTITIES = frozenset({b"amp", b"lt", b"gt", b"apos", b"quot"})
ENTITY_REF_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]+);")
PARTIAL_ENTITY_TAIL_RE = re.compile(rb"&[A-Za-z][A-Za-z0-9]*$")
MAX_ENTITY_NAME_LENGTH = max(len(name) for name in name2codepoint)

UPPERCASE_ENTITY_CODEPOINTS: dict[str, int] = {}
for entity_name, codepoint in name2codepoint.items():
    if entity_name[:1].isupper():
        UPPERCASE_ENTITY_CODEPOINTS[entity_name.upper()] = codepoint
for entity_name, codepoint in name2codepoint.items():
    UPPERCASE_ENTITY_CODEPOINTS.setdefault(entity_name.upper(), codepoint)

# Front-file `status` attribute values on `<exch:exchange-document>`.
STATUS_AMEND = "A"
STATUS_DELETE = "D"
STATUS_CREATE = "C"

# Type alias for a single document record.
Record = list[str]


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def processed_doc_number(text: str | bytes) -> bytes:
    """Re-normalise an arbitrary publication number to `CC + digits` form.

    Mirrors what the API endpoint will do when it receives the country
    code and the number as two separate parameters: strip whitespace,
    take the first two characters as the country code, strip leading
    zeros from the remainder, and upper-case the result. The output has
    the same shape as the canonical primary key produced by the
    back-file extractor, which lets the alias sub-DB chain into
    `docs_db` with no further normalisation.

    Returns an empty bytes object when the input has fewer than three
    non-whitespace characters (no country code + at least one digit).
    """
    if isinstance(text, str):
        text = text.encode()
    # Single C-level pass: delete all whitespace characters using
    # a pre-computed deletion table for whitespace characters.
    s = text.translate(None, b" \t\n\r\x0b\x0c/-,")
    # Strip a trailing `.\d+` suffix.
    dot = s.rfind(b".")
    if dot > 0 and s[dot + 1 :].isdigit():
        s = s[:dot]
    if s[:2] in VALID_CC:
        cc, rest = s[:2], s[2:]
        return (cc + rest.lstrip(b"0")).upper()
    else:
        return s.upper()


def open_xml(path: Path) -> IO[bytes]:
    """Open `path` as a binary stream, transparently decompressing gzip content."""
    with path.open("rb") as fh:
        magic = fh.read(len(GZIP_MAGIC))
    if magic == GZIP_MAGIC:
        return gzip.open(path, "rb")
    return path.open("rb")


def _html_entity_codepoint(name: str) -> int | None:
    """Return the Unicode codepoint for an HTML/SGML-style entity name."""
    codepoint = name2codepoint.get(name)
    if codepoint is not None:
        return codepoint
    return UPPERCASE_ENTITY_CODEPOINTS.get(name)


def normalize_xml_entities(data: bytes) -> bytes:
    """Rewrite non-XML named entities to XML-safe numeric character refs.

    DOCDB files occasionally contain SGML/HTML entity names such as
    `&EACUTE;`. Those are not predefined in XML, so ElementTree rejects the
    whole file. Numeric character refs are valid XML and keep the text content.
    Unknown named entities are escaped as literal text instead of aborting the
    parse.
    """

    def replace(match: re.Match[bytes]) -> bytes:
        name_bytes = match.group(1)
        if name_bytes in XML_BUILTIN_ENTITIES:
            return match.group(0)

        name = name_bytes.decode("ascii")
        codepoint = _html_entity_codepoint(name)
        if codepoint is not None:
            return f"&#{codepoint};".encode("ascii")
        return b"&amp;" + name_bytes + b";"

    return ENTITY_REF_RE.sub(replace, data)


class EntityNormalizingReader:
    """Binary reader that normalizes named entities without loading the file."""

    def __init__(self, fh: IO[bytes]) -> None:
        self._fh = fh
        self._pending = b""
        self._output = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            output = self._output
            data = self._pending + self._fh.read()
            self._output = b""
            self._pending = b""
            return output + normalize_xml_entities(data)

        while len(self._output) < size:
            chunk = self._fh.read(max(size, 8192))
            if not chunk:
                if self._pending:
                    self._output += normalize_xml_entities(self._pending)
                    self._pending = b""
                break

            data = self._pending + chunk
            safe_len = _entity_safe_prefix_len(data)
            self._output += normalize_xml_entities(data[:safe_len])
            self._pending = data[safe_len:]

        result = self._output[:size]
        self._output = self._output[size:]
        return result


def _entity_safe_prefix_len(data: bytes) -> int:
    """Return a prefix length that does not split a potential entity ref."""
    last_amp = data.rfind(b"&")
    if last_amp == -1:
        return len(data)
    if data.find(b";", last_amp) != -1:
        return len(data)

    tail = data[last_amp:]
    if len(tail) <= MAX_ENTITY_NAME_LENGTH + 2 and PARTIAL_ENTITY_TAIL_RE.fullmatch(tail):
        return last_amp
    return len(data)


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


class DocdbRecordTarget:
    """lxml parser target that extracts records without building XML trees."""

    def __init__(self) -> None:
        self.records: list[tuple[str, Record, str]] = []
        self._in_doc = False
        self._country = ""
        self._doc_number = ""
        self._kind = ""
        self._date_publ = ""
        self._status = ""
        self._first_inventor = ""
        self._seen_first_inventor = False
        self._in_matching_inventor = False
        self._collecting_name = False
        self._name_parts: list[str] = []

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == TAG_EXCHANGE_DOCUMENT:
            self._in_doc = True
            self._country = attrs.get("country", "").strip()
            self._doc_number = attrs.get("doc-number", "").strip()
            self._kind = attrs.get("kind", "").strip()
            self._date_publ = attrs.get("date-publ", "").strip()
            self._status = attrs.get("status", "").strip()
            self._first_inventor = ""
            self._seen_first_inventor = False
            self._in_matching_inventor = False
            self._collecting_name = False
            self._name_parts = []
            return

        if not self._in_doc or self._seen_first_inventor:
            return

        if tag == TAG_INVENTOR:
            self._in_matching_inventor = attrs.get("sequence") == "1" and attrs.get("data-format") == "docdb"
            self._name_parts = []
        elif self._in_matching_inventor and tag == TAG_NAME:
            self._collecting_name = True

    def data(self, data: str) -> None:
        if self._collecting_name:
            self._name_parts.append(data)

    def end(self, tag: str) -> None:
        if not self._in_doc:
            return

        if self._collecting_name and tag == TAG_NAME:
            self._collecting_name = False
        elif self._in_matching_inventor and tag == TAG_INVENTOR:
            self._first_inventor = "".join(self._name_parts).strip()
            self._seen_first_inventor = True
            self._in_matching_inventor = False
        elif tag == TAG_EXCHANGE_DOCUMENT:
            if self._country and self._doc_number:
                key = f"{self._country}{self._doc_number}"
                docdb_id = f"{self._country}{self._doc_number}{self._kind}"
                self.records.append((key, [docdb_id, self._first_inventor, self._date_publ], self._status))
            self._in_doc = False

    def close(self) -> list[tuple[str, Record, str]]:
        return self.records


def iter_documents_in_file(xml_path: Path) -> Iterator[tuple[str, Record, str]]:
    """Stream `(key, record, status)` triples from a single XML file.

    Uses an lxml target parser to avoid building XML element trees. The
    parser stores extracted records, not source XML subtrees.
    """
    yield from parse_documents_in_file(xml_path)


def parse_documents_in_file(xml_path: Path) -> list[tuple[str, Record, str]]:
    """Return all document triples from one XML file."""
    with open_xml(xml_path) as fh:
        parser = LET.XMLParser(target=DocdbRecordTarget(), recover=True, huge_tree=True)
        return LET.parse(EntityNormalizingReader(fh), parser)


def iter_all_documents(xml_paths: list[Path]) -> Iterator[tuple[str, Record, str]]:
    """Stream `(key, record, status)` triples from every input XML file."""
    for path in xml_paths:
        logger.info(f"processing {path}")
        try:
            yield from iter_documents_in_file(path)
        except LET.ParseError as exc:
            logger.error(f"XML parse error in {path}: {exc}; continuing with next file")
            continue
