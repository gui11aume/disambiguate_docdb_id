"""Text and XML normalization helpers for DOCDB ingest."""

from __future__ import annotations

import re
from typing import IO

from docdb_id.country_codes import VALID_CC


def normalize_alternate_identifier(text: str | bytes) -> bytes:
    """Normalize publication identifier to `CC + CLEANED_SUFFIX`.

    Mirrors what the API endpoint does when it receives the country
    code and the number as two separate parameters: strip whitespace,
    take the first two characters as the country code, strip leading
    zeros from the remainder, and upper-case the result. The output has
    the same shape as the canonical primary key produced by the
    backfile extractor, which lets the alias sub-DB chain into
    `docs_db` with no further normalisation.

    Args:
        text: Publication identifier as a string or bytes.

    Returns:
        Normalized `CC + CLEANED_SUFFIX` bytes when the first two
        characters are a valid country code, or the cleaned and
        upper-cased input unchanged otherwise.

    Raises:
        ValueError: If the country code is invalid.
    """
    if isinstance(text, str):
        text = text.encode()
    # Remove white spaces, hyphens, commas and periods.
    s = text.translate(None, b" \t\n\r\x0b\x0c/-,")
    # Remove trailing version numbers like ".1", ".2", etc.
    dot = s.rfind(b".")
    if dot > 0 and s[dot + 1 :].isdigit():
        s = s[:dot]
    if s[:2] in VALID_CC:
        cc, suffix = s[:2], s[2:]
        return (cc + suffix.lstrip(b"0")).upper()
    return s.lstrip(b"0").upper()


class EntityNormalizingReader:
    """Transforming binary reader that strips invalid XML entities on the fly.

    DOCDB frontfile XML is not quite well-formed XML: it carries SGML/HTML
    named entities such as `&EACUTE;` that XML parsers do not recognise.
    Feeding those bytes directly to lxml makes the parser reject the whole
    document. This wrapper sits between a raw file handle and the parser and
    rewrites entity references as bytes are read, so ingest can stream large
    files without loading them into memory first.

    The rewrite rules are deliberately lossy:

    * XML predefined entities (`&amp;`, `&lt;`, `&gt;`, `&apos;`,
      `&quot;`) are kept and rewritten to canonical lowercase form.
    * Every other named entity (`&EACUTE;`, `&eacute;`, etc.) is removed
      entirely — the reference is deleted, not decoded to a Unicode character.
      That is safe for the fields we parse: inventor names in DOCDB do not
      contain these entities.

    `read()` behaves like a file object but the returned bytes may be shorter
    than the underlying stream because entities are stripped. Chunked reads add
    another wrinkle: an entity reference must not be split across `read()`
    calls, so a trailing `&…` fragment that might still grow into a complete
    `&name;` is held back in an internal buffer until the next chunk arrives
    or EOF flushes it. Concatenating successive `read()` results therefore
    reproduces the fully normalised byte stream.

    Typical use::

        with open_xml(path) as raw:
            reader = EntityNormalizingReader(raw)
            while chunk := reader.read(CHUNK):
                parser.feed(chunk)
    """

    XML_BUILTIN_ENTITIES = frozenset({b"amp", b"lt", b"gt", b"apos", b"quot"})
    ENTITY_REF_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]+);")
    PARTIAL_ENTITY_TAIL_RE = re.compile(rb"&[A-Za-z][A-Za-z0-9]*$")
    MAX_ENTITY_NAME_LENGTH = 32

    def __init__(self, f: IO[bytes]) -> None:
        self._f = f
        self._pending = b""
        self._output = b""

    def _normalize_xml_entities(self, data: bytes) -> bytes:
        """Strip non-XML named entities so ElementTree can parse DOCDB files.

        DOCDB files occasionally contain SGML/HTML entity names such as
        `&EACUTE;`. Those are not predefined in XML, so ElementTree rejects the
        whole file. They are removed entirely; inventor names do not contain
        them. XML predefined entities (`&amp;`, `&lt;`, etc.) are kept in
        canonical lowercase form.

        Args:
            data: Raw bytes of the XML document.

        Returns:
            Bytes with non-XML named entities removed.
        """

        def replace(match: re.Match[bytes]) -> bytes:
            name_bytes = match.group(1)
            name_lower = name_bytes.lower()
            if name_lower in self.XML_BUILTIN_ENTITIES:
                return b"&" + name_lower + b";"
            return b""

        return self.ENTITY_REF_RE.sub(replace, data)

    def _entity_safe_prefix_len(self, data: bytes) -> int:
        """Return a prefix length that does not split a potential entity ref.

        Args:
            data: Raw bytes chunk.

        Returns:
            Length of the safe prefix that can be normalised without
            splitting an entity reference across chunk boundaries.
        """
        last_amp = data.rfind(b"&")
        # No entity reference in this buffer; safe to normalize all of it.
        if last_amp == -1:
            return len(data)
        # Entity reference complete in this buffer; safe to normalize all of it.
        if data.find(b";", last_amp) != -1:
            return len(data)

        tail = data[last_amp:]
        if (
            len(tail) <= self.MAX_ENTITY_NAME_LENGTH + 2
            and self.PARTIAL_ENTITY_TAIL_RE.fullmatch(tail)
        ):
            return last_amp
        return len(data)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            output = self._output
            data = self._pending + self._f.read()
            self._output = b""
            self._pending = b""
            return output + self._normalize_xml_entities(data)

        while len(self._output) < size:
            chunk = self._f.read(max(size, 8192))
            if not chunk:
                if self._pending:
                    self._output += self._normalize_xml_entities(self._pending)
                    self._pending = b""
                break

            data = self._pending + chunk
            safe_len = self._entity_safe_prefix_len(data)
            self._output += self._normalize_xml_entities(data[:safe_len])
            self._pending = data[safe_len:]

        result = self._output[:size]
        self._output = self._output[size:]
        return result
