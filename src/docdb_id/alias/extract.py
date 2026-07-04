"""Project the main 6-column backfile TSV down to the alias 3-column input.

The main TSV has columns:

    key \t docdb_id \t orig_doc_number \t inventor \t date_publ \t family_id

For every row we form the candidate identifier `key[:2] + orig_doc_number` -
i.e. the row's country code prepended to the office's native publication number
- and re-normalise it with `docdb_id.normalize.processed_doc_number`. The API
endpoint will receive the country code and the number as two separate parameters
and combine them in the same way, so alias keys built here are byte-for-byte
identical to what the endpoint will compute at lookup time.

Each surviving row is emitted as::

    processed(key[:2] + orig_doc_number) \t key \t date_publ

The trailing `date_publ` (or `99999999` when the source date is empty) is a
sort aid, not loader input: the alias stage sorts on `(alias, date_publ)`
ascending and keeps the first row per alias, so a genuine collision resolves to
the key with the oldest publication date. The date column is stripped before the
row reaches `docdb_id.store.alias`.

We skip rows where:
  * `orig_doc_number` is empty or normalises to nothing;
  * the normalised alias does not match `[A-Z][A-Z][A-Z0-9][-A-Z0-9]*`;
  * the normalised alias collapses onto `key` itself (a direct probe of the
    docs DB already resolves the query).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator

logger = logging.getLogger("docdb_id.alias.extract")
from dataclasses import dataclass
from datetime import date

from docdb_id.normalize import processed_doc_number

BRAZIL_C_RE = re.compile(rb"^C[0-9]+$")
BRAZIL_PI_RE = re.compile(rb"^PI[0-9]+$")
CUBA_P_RE = re.compile(rb"^P[0-9]+$")
DOMINICAN_REPUBLIC_P_RE = re.compile(rb"^P[0-9]+$")
FINLAND_U_RE = re.compile(rb"^U[0-9]+$")
GEORGIA_PUYB_RE = re.compile(rb"^[PU][0-9]+[BY]$")
INDIA_MULTI_RE = re.compile(rb"^[0-9]+[A-Z]{2,}[0-9]{4}$")
IRELAND_S_RE = re.compile(rb"^S[0-9]+$")
ITALY_U_RE = re.compile(rb"^[0-9]+U[0-9]+$")
ITALY_1980_RE = re.compile(rb"^198[0-9][AU][0-9]+$")
ITALY_PROVINCE_RE = re.compile(rb"^(?!IT)[A-Z]{2}[0-9]{4}[AU][0-9]+$")
JORDAN_P_RE = re.compile(rb"^P[0-9]+$")
JAPAN_D_RE = re.compile(rb"^D[0-9]+$")
MALTA_P_RE = re.compile(rb"^P[0-9]+$")
NICARAGUA_A_RE = re.compile(rb"^[0-9]+A$")
ROMANIA_A_RE = re.compile(rb"^A[0-9]+$")
SERBIA_U_RE = re.compile(rb"^[0-9]+U$")
SAN_MARINO_PT_RE = re.compile(rb"^[PT][0-9]+$")
SAUDI_ARABIA_RE = re.compile(rb"^[0-9]+$")
TUNISIA_P_RE = re.compile(rb"^P[0-9]+$")
US_D_RE = re.compile(rb"^D[0-9]+$")
US_H_RE = re.compile(rb"^H[0-9]+$")
SOUTH_AFRICA_A_RE = re.compile(rb"^A[0-9]+$")


JUST_NUMBERS_RE = re.compile(rb"^[0-9]+$")
ALIAS_RE = re.compile(rb"^[A-Z][A-Z][A-Z0-9][A-Z0-9][A-Z0-9]+$")
ZERO_STRIPPED_KEY_RE = re.compile(rb"^([A-Z]{2})([0-9]{4})([0-9]{6})$")
WO_TWO_DIGIT_YEAR_KEY_RE = re.compile(rb"^WO([0-9]{2})([0-9]{5})$")
JP_ERA_KEY_RE = re.compile(rb"^JP[HS]([0-9]{3,})$")
JP_ERA_PADDED_RE = re.compile(rb"^JP([HS])([0-9]{2})([1-9][0-9]{0,4})$")
MIN_ZERO_STRIPPED_YEAR = 1850
MAX_ZERO_STRIPPED_YEAR = date.today().year
MIN_WO_YEAR = 1978
MAX_WO_YEAR = date.today().year

COUNTRY_RULES: dict[bytes, tuple[tuple[re.Pattern[bytes], ...], int]] = {
    b"BR": ((BRAZIL_C_RE, BRAZIL_PI_RE), 0),
    b"CU": ((CUBA_P_RE,), 0),
    b"DO": ((DOMINICAN_REPUBLIC_P_RE,), 0),
    b"FI": ((FINLAND_U_RE,), 0),
    b"GE": ((GEORGIA_PUYB_RE,), 1),
    b"IN": ((INDIA_MULTI_RE,), 0),
    b"IE": ((IRELAND_S_RE,), 0),
    b"IT": ((ITALY_1980_RE, ITALY_U_RE, ITALY_PROVINCE_RE), 0),
    b"JO": ((JORDAN_P_RE,), 0),
    b"JP": ((JAPAN_D_RE,), 0),
    b"MT": ((MALTA_P_RE,), 0),
    b"NI": ((NICARAGUA_A_RE,), 1),
    b"RO": ((ROMANIA_A_RE,), 0),
    b"RS": ((SERBIA_U_RE,), 1),
    b"SM": ((SAN_MARINO_PT_RE,), 0),
    b"SA": ((SAUDI_ARABIA_RE,), 0),
    b"TN": ((TUNISIA_P_RE,), 0),
    b"US": ((US_D_RE, US_H_RE), 0),
    b"ZA": ((SOUTH_AFRICA_A_RE,), 0),
}


def zero_stripped_key_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield key aliases with leading zeros removed from a CCYYYYNNNNNN key."""
    match = ZERO_STRIPPED_KEY_RE.fullmatch(key)
    if match is None:
        return

    cc, year_bytes, number = match.groups()
    year = int(year_bytes)
    if not (MIN_ZERO_STRIPPED_YEAR <= year <= MAX_ZERO_STRIPPED_YEAR):
        return

    max_strip = len(number) - len(number.lstrip(b"0"))
    if max_strip == len(number):
        max_strip -= 1

    for n_strip in range(1, max_strip + 1):
        yield cc + year_bytes + number[n_strip:]


def wo_two_digit_year_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield WOYYYY aliases for a WOYYNNNNN key."""
    match = WO_TWO_DIGIT_YEAR_KEY_RE.fullmatch(key)
    if match is None:
        return

    yy_bytes, number = match.groups()
    yy = int(yy_bytes)
    yyyy = 1900 + yy if yy >= MIN_WO_YEAR % 100 else 2000 + yy
    if not (MIN_WO_YEAR <= yyyy <= MAX_WO_YEAR):
        return

    yyyy_bytes = str(yyyy).encode()
    yield b"WO" + yyyy_bytes + b"0" + number
    yield b"WO" + yyyy_bytes + number


def jp_era_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield JP... aliases for a JPH/JPS key by dropping the era letter.

    JP DOCDB keys carry a Heisei (`H`) or Showa (`S`) era prefix that
    external systems frequently omit when quoting the same publication. We emit
    `JP` + suffix as the canonical-form alias, plus each progressively
    zero-stripped variant. The 3-digit lower bound on the suffix keeps every
    emitted alias at or above the 5-character minimum enforced by `ALIAS_RE`.
    """
    match = JP_ERA_KEY_RE.fullmatch(key)
    if match is None:
        return

    suffix = match.group(1)
    yield b"JP" + suffix

    n_zeros = len(suffix) - len(suffix.lstrip(b"0"))
    for n_strip in range(1, n_zeros + 1):
        if len(suffix) - n_strip < 3:
            break
        yield b"JP" + suffix[n_strip:]


def jp_era_padded_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield zero-padded JP... aliases for a JPH/JPS key.

    Canonical form is `JP{H,S}YY<doc>` with `<doc>` having leading zeros
    stripped (1-5 digits). External systems often quote the same publication with
    the doc number padded to a fixed 6-digit width. For each target width from
    `len(doc)+1` up to 6, emit both the era-kept and era-stripped variants. JP
    is the one CC where leading zeros after the country code are preserved by the
    lookup, so the era-stripped variants are emitted verbatim.
    """
    match = JP_ERA_PADDED_RE.fullmatch(key)
    if match is None:
        return

    era, yy, doc = match.groups()
    for target_len in range(len(doc) + 1, 7):
        padded = doc.rjust(target_len, b"0")
        yield b"JP" + era + yy + padded
        yield b"JP" + yy + padded


# Publication date for rows whose source `date_publ` is empty. It sorts after
# every real 8-digit date under `LC_ALL=C`, so when the alias stage breaks ties
# on the date (oldest wins), a row with a known date always beats a row with a
# missing one.
MISSING_DATE = b"99999999"


def _normalize_alias(alias: bytes) -> bytes | None:
    """Return normalized alias bytes, or None if the alias is too short to store."""
    if alias[:2] != b"JP":
        body = alias[2:].lstrip(b"0")
        if len(body) < 3:
            return None
        alias = alias[:2] + body
    return alias


def key_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield normalized key-derived alias byte-strings for *key*."""
    for synonym in zero_stripped_key_synonyms(key):
        normalized = _normalize_alias(synonym)
        if normalized is not None:
            yield normalized
    for synonym in wo_two_digit_year_synonyms(key):
        normalized = _normalize_alias(synonym)
        if normalized is not None:
            yield normalized
    for synonym in jp_era_synonyms(key):
        normalized = _normalize_alias(synonym)
        if normalized is not None:
            yield normalized
    for synonym in jp_era_padded_synonyms(key):
        normalized = _normalize_alias(synonym)
        if normalized is not None:
            yield normalized


@dataclass(frozen=True)
class OrigAliasBatch:
    """Orig-derived aliases for one document plus skip counters for emit()."""

    aliases: tuple[bytes, ...]
    skipped_equal: bool = False
    skipped_pattern: bool = False


def orig_aliases(key: bytes, orig: bytes) -> OrigAliasBatch:
    """Return orig-derived normalized aliases for one document."""
    if not orig or len(key) < 2:
        return OrigAliasBatch(())

    cc = key[:2]
    if cc + orig.lstrip(b"0") == key:
        return OrigAliasBatch((), skipped_equal=True)

    alias = processed_doc_number(orig)
    if not alias:
        return OrigAliasBatch(())
    if alias == key:
        return OrigAliasBatch((), skipped_equal=True)
    if cc + alias == key:
        return OrigAliasBatch((), skipped_equal=True)

    country_rule = COUNTRY_RULES.get(cc)
    if country_rule is not None:
        patterns, trim_suffix = country_rule
        if any(pattern.fullmatch(alias) for pattern in patterns):
            country_alias = cc + (alias[:-trim_suffix] if trim_suffix else alias)
            if country_alias == key:
                return OrigAliasBatch((), skipped_equal=True)
            normalized = _normalize_alias(country_alias)
            if normalized is None:
                return OrigAliasBatch(())
            return OrigAliasBatch((normalized,))

    if JUST_NUMBERS_RE.fullmatch(alias) and len(alias) > 3:
        alias = cc + alias
    if not ALIAS_RE.fullmatch(alias):
        logger.debug("skipped pattern: %r %r (%r)", orig, alias, key)
        return OrigAliasBatch((), skipped_pattern=True)

    normalized = _normalize_alias(alias)
    if normalized is None:
        return OrigAliasBatch(())
    return OrigAliasBatch((normalized,))


def aliases_for_document(key: bytes, orig: bytes) -> Iterator[bytes]:
    """Yield every normalized alias for one document (key synonyms + orig aliases)."""
    yield from key_synonyms(key)
    yield from orig_aliases(key, orig).aliases


def _write_alias(out, alias: bytes, key: bytes, date_publ: bytes) -> bool:
    """Write one alias row to *out*. Returns True on success."""
    out.write(alias + b"\t" + key + b"\t" + (date_publ or MISSING_DATE) + b"\n")
    return True


def emit(src, out) -> tuple[int, int, int, int, int, int, int, int]:
    """Read 6-col TSV rows from *src* and write 3-col alias input to *out*.

    Returns `(n_in, n_out, n_zero_stripped, n_wo_two_digit_year, n_jp_era,
    n_jp_era_padded, n_skipped_equal, n_skipped_pattern)` so the caller can
    sanity check coverage.
    """
    n_in = 0
    n_out = 0
    n_zero_stripped = 0
    n_wo_two_digit_year = 0
    n_jp_era = 0
    n_jp_era_padded = 0
    n_skipped_equal = 0
    n_skipped_pattern = 0

    for raw in src:
        n_in += 1
        line = raw.rstrip(b"\n") if isinstance(raw, bytes) else raw.rstrip("\n").encode()
        parts = line.split(b"\t", 5)
        if len(parts) != 6:
            logger.warning("malformed line %d: %r", n_in, line[:80])
            continue

        key = parts[0]
        orig = parts[2]
        date_publ = parts[4]
        if len(key) < 2:
            continue

        for synonym in zero_stripped_key_synonyms(key):
            normalized = _normalize_alias(synonym)
            if normalized is not None and _write_alias(out, normalized, key, date_publ):
                n_out += 1
                n_zero_stripped += 1

        for synonym in wo_two_digit_year_synonyms(key):
            normalized = _normalize_alias(synonym)
            if normalized is not None and _write_alias(out, normalized, key, date_publ):
                n_out += 1
                n_wo_two_digit_year += 1

        for synonym in jp_era_synonyms(key):
            normalized = _normalize_alias(synonym)
            if normalized is not None and _write_alias(out, normalized, key, date_publ):
                n_out += 1
                n_jp_era += 1

        for synonym in jp_era_padded_synonyms(key):
            normalized = _normalize_alias(synonym)
            if normalized is not None and _write_alias(out, normalized, key, date_publ):
                n_out += 1
                n_jp_era_padded += 1

        orig_batch = orig_aliases(key, orig)
        if orig_batch.skipped_equal:
            n_skipped_equal += 1
        if orig_batch.skipped_pattern:
            n_skipped_pattern += 1
        for alias in orig_batch.aliases:
            if _write_alias(out, alias, key, date_publ):
                n_out += 1

    return (
        n_in,
        n_out,
        n_zero_stripped,
        n_wo_two_digit_year,
        n_jp_era,
        n_jp_era_padded,
        n_skipped_equal,
        n_skipped_pattern,
    )
