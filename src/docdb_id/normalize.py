"""Text and XML normalization helpers for DOCDB ingest."""

from __future__ import annotations

import gzip
import re
from html.entities import name2codepoint
from pathlib import Path
from typing import IO

from docdb_id.country_codes import VALID_CC

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


def processed_doc_number(text: str | bytes) -> bytes:
    """Re-normalise an arbitrary publication number to `CC + digits` form.

    Mirrors what the API endpoint will do when it receives the country
    code and the number as two separate parameters: strip whitespace,
    take the first two characters as the country code, strip leading
    zeros from the remainder, and upper-case the result. The output has
    the same shape as the canonical primary key produced by the
    backfile extractor, which lets the alias sub-DB chain into
    `docs_db` with no further normalisation.

    Returns an empty bytes object when the input has fewer than three
    non-whitespace characters (no country code + at least one digit).
    """
    if isinstance(text, str):
        text = text.encode()
    s = text.translate(None, b" \t\n\r\x0b\x0c/-,")
    dot = s.rfind(b".")
    if dot > 0 and s[dot + 1 :].isdigit():
        s = s[:dot]
    if s[:2] in VALID_CC:
        cc, rest = s[:2], s[2:]
        return (cc + rest.lstrip(b"0")).upper()
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
