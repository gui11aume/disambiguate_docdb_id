"""Project the main 6-column backfile TSV down to the alias 3-column input.

The main TSV has columns::

    key \t docdb_id \t alt_doc_number \t inventor \t date_publ \t family_id

For each input row, `emit()` writes zero or more alias rows. Aliases come from
two independent sources, both normalised to the same `CC + body` shape the API
endpoint builds at lookup time:

* **Key synonyms** — derived from the canonical `key` alone (zero-stripped
  `CCYYYY0...0NNNNNN` variants, WO two-digit-year expansions, JP era stripping,
  and JP era-padded variants). See `key_synonyms()`.
* **Alternate-ID aliases** — derived from `alt_doc_number` via
  `normalize_alternate_identifier`, per-country heuristics, and a bare-digits
  fallback that prepends the row's country code. See `alt_alias()`.

Each emitted row has the form::

    alias \t key \t date_publ

where `alias` is a normalised byte-string. A single document can therefore
produce several output rows. The trailing `date_publ` (or `99999999` when the
source date is empty) is a sort aid, not loader input: the alias stage sorts on
`(alias, date_publ)` ascending and keeps the first row per alias, so a genuine
collision resolves to the key with the oldest publication date. The date column
is stripped before the row reaches `docdb_id.store.alias`.

Alternate-ID aliases are skipped when:

* `alt_doc_number` is empty or normalises to nothing;
* the normalised alias collapses onto `key` (or `key[:2] + alias`) — a direct
  probe of the docs DB already resolves the query;
* the alias does not match `CC_PLUS_NUMBER_RE` or its country code is not in
  `VALID_CC`.

Key synonyms are governed by their own generators and `normalize_key()`; they
never consult `alt_doc_number`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from ..country_codes import VALID_CC
from ..normalize import normalize_alternate_identifier
from ..utils import normalize_key

logger = logging.getLogger("docdb_id.alias.extract")

# Country-specific rules for aliases.
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

# Generic rules for aliases.
JUST_DIGITS_RE = re.compile(rb"^[0-9]+$")
CC_PLUS_NUMBER_RE = re.compile(rb"^[A-Z]{2}[A-Z0-9]{3,}$")

# Synonym generators.
CC_YYYY_NNNNNN_KEY_RE = re.compile(rb"^([A-Z]{2})([0-9]{4})([0-9]{6,})$")
WO_TWO_DIGIT_YEAR_KEY_RE = re.compile(rb"^WO([0-9]{2})([0-9]{5})$")
JP_ERA_KEY_RE = re.compile(rb"^JP([HS])([0-9]{2})([0-9]{1,6})$")
MIN_ZERO_STRIPPED_YEAR = 1850
MAX_ZERO_STRIPPED_YEAR = date.today().year
MIN_WO_YEAR = 1978
MAX_WO_YEAR = date.today().year

COUNTRY_RULES: dict[bytes, tuple[tuple[re.Pattern[bytes], ...], int]] = {
    # Each value is `(patterns, trim_suffix)`: when a normalised `alias` matches
    # any `patterns`, form `cc + alias[:-trim_suffix]` (or `cc + alias` when
    # `trim_suffix` is 0) and emit that as the country-specific alias.
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


def zero_stripped_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield key aliases with zeros removed from CCYYYY0...0NNNNNN.

    For example, `EP2010001234` yields `EP201001234`, then `EP20101234`,
    but not `EP2011234`.

    Args:
        key: A key in CCYYYY0...0NNNNNN format.

    Yields:
        Key aliases with zeros progressively removed from the number suffix.
    """
    match = CC_YYYY_NNNNNN_KEY_RE.fullmatch(key)
    if match is None:
        return

    cc, year_bytes, number = match.groups()
    if cc not in VALID_CC:
        return
    year = int(year_bytes)
    if not (MIN_ZERO_STRIPPED_YEAR <= year <= MAX_ZERO_STRIPPED_YEAR):
        return

    max_strip = len(number) - len(number.lstrip(b"0"))
    if max_strip == len(number):
        max_strip -= 1

    for n_strip in range(1, max_strip + 1):
        yield cc + year_bytes + number[n_strip:]


def wo_two_digit_year_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield WOYYYY aliases for a WOYYNNNNN key.

    For example, `WO9801234` yields `WO1998001234`, then `WO199801234`,
    then `WO19981234`. `WO1201234` yields `WO2012001234`, then
    `WO201201234`, then `WO20121234`.

    Args:
        key: A WO key with a two-digit year (WOYYNNNNN format).

    Yields:
        WOYYYY aliases with a leading-zero-padded document number, the
        four-digit-year form of the key, and further aliases with zeros
        progressively stripped from the left of the number until none remain.
    """
    match = WO_TWO_DIGIT_YEAR_KEY_RE.fullmatch(key)
    if match is None:
        return

    yy_bytes, number = match.groups()
    yy = int(yy_bytes)
    yyyy = 1900 + yy if yy >= MIN_WO_YEAR % 100 else 2000 + yy
    if not (MIN_WO_YEAR <= yyyy <= MAX_WO_YEAR):
        return

    yyyy_bytes = str(yyyy).encode()
    yield b"WO" + yyyy_bytes + b"0" + number  # 10 digits.
    yield b"WO" + yyyy_bytes + number  # 9 digits.

    max_strip = len(number) - len(number.lstrip(b"0"))
    if max_strip == len(number):
        max_strip -= 1
    for n_strip in range(1, max_strip + 1):
        yield b"WO" + yyyy_bytes + number[n_strip:]



def jp_era_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield JP... aliases for a JPH/JPS key by dropping the era letter.

    JP DOCDB keys carry a Heisei (`H`) or Showa (`S`) era prefix that
    external systems frequently omit when quoting the same publication. We emit
    `JP` + suffix as the canonical-form alias, plus each progressively
    zero-stripped variant. The 3-digit lower bound on the suffix keeps every
    emitted alias at or above the 5-character minimum.

    For example, `JPH1001234` yields `JPH10001234`, then `JP10001234`, then
    `JP101234`, then `JPH101234`, then `JP101234`.

    Args:
        key: A JPH or JPS key with an era prefix.

    Yields:
        JP + suffix aliases with progressively zero-stripped variants.
    """
    match = JP_ERA_KEY_RE.fullmatch(key)
    if match is None:
        return

    era, yy, doc = match.groups()
    # Add progressive zeros to the document number.
    for target_len in range(len(doc) + 1, 7):
        padded = doc.rjust(target_len, b"0")
        yield b"JP" + era + yy + padded
        yield b"JP" + yy + padded

    yield b"JP" + yy + doc

    max_strip = len(doc) - len(doc.lstrip(b"0"))
    for n_strip in range(1, max_strip + 1):
        if len(doc) - n_strip < 3:
            break
        yield b"JP" + era + yy + doc[n_strip:]
        yield b"JP" + yy + doc[n_strip:]


# Publication date for rows whose source `date_publ` is empty. It sorts after
# every real 8-digit date under `LC_ALL=C`, so when the alias stage breaks ties
# on the date (oldest wins), a row with a known date always beats a row with a
# missing one.
MISSING_DATE = b"99999999"


def key_synonyms(key: bytes) -> Iterator[bytes]:
    """Yield normalized key-derived alias byte-strings for key.

    Args:
        key: A DOCDB key to generate synonyms for.

    Yields:
        Normalized alias byte-strings from zero-stripped, WO two-digit year,
        JP era, and JP era-padded synonym generators.
    """
    for synonym in zero_stripped_synonyms(key):
        yield normalize_key(synonym[:2], synonym[2:])
    for synonym in wo_two_digit_year_synonyms(key):
        yield normalize_key(synonym[:2], synonym[2:])
    for synonym in jp_era_synonyms(key):
        yield normalize_key(synonym[:2], synonym[2:])


@dataclass(frozen=True)
class AltAliasOutcome:
    """Result of trying to derive one alias from an alternate ID.

    Args:
        alias: The normalized alias byte-string, or None when skipped/empty.
        skipped_equal: True when the alternate ID matched the key or
            country+alias (already resolvable via the docs DB).
        skipped_pattern: True when the alternate ID failed CC_PLUS_NUMBER_RE
            validation.
    """

    alias: bytes | None = None
    skipped_equal: bool = False
    skipped_pattern: bool = False


def alt_alias(key: bytes, alt: bytes) -> AltAliasOutcome:
    """Derive one normalized alias from an alternate identifier.

    Args:
        key: The DOCDB key for the document (country context + equality checks).
        alt: The alternate identifier to derive an alias from.

    Returns:
        An AltAliasOutcome with the alias (if any) and skip flags for emit().
    """
    if not alt or len(key) < 2:
        return AltAliasOutcome()

    cc = key[:2]
    if cc + alt.lstrip(b"0") == key:
        return AltAliasOutcome(skipped_equal=True)

    alias = normalize_alternate_identifier(alt)
    # The alternate ID is the same as the key.
    if alias == key:
        return AltAliasOutcome(skipped_equal=True)
    # The alternate ID is the identifier without country code.
    if cc + alias == key:
        return AltAliasOutcome(skipped_equal=True)

    # Process country-specific rules.
    country_rule = COUNTRY_RULES.get(cc)
    if country_rule is not None:
        patterns, trim_suffix = country_rule
        if any(pattern.fullmatch(alias) for pattern in patterns):
            country_alias = cc + (alias[:-trim_suffix] if trim_suffix else alias)
            if country_alias == key:
                return AltAliasOutcome(skipped_equal=True)
            return AltAliasOutcome(alias=normalize_key(country_alias[:2], country_alias[2:]))

    # If the alternate ID is just digits, assume it's the document number
    # and add the country code to get the full alias.
    if JUST_DIGITS_RE.fullmatch(alias) and len(alias) > 3:
        alias = cc + alias
    # At that point, the alias must be in the
    # form CC + number, otherwise, stop trying.
    if not CC_PLUS_NUMBER_RE.fullmatch(alias) or alias[:2] not in VALID_CC:
        logger.debug("skipped pattern: %r %r (%r)", alt, alias, key)
        return AltAliasOutcome(skipped_pattern=True)

    return AltAliasOutcome(alias=normalize_key(alias[:2], alias[2:]))


def aliases_for_document(key: bytes, alt: bytes) -> Iterator[bytes]:
    """Yield every normalized alias for one document (key synonyms + alt IDs).

    Args:
        key: The DOCDB key for the document.
        alt: The alternate document number.

    Yields:
        Normalized alias byte-strings from key synonyms and alternate IDs.
    """
    yield from key_synonyms(key)
    outcome = alt_alias(key, alt)
    if outcome.alias is not None:
        yield outcome.alias


def _write_alias(out, alias: bytes, key: bytes, date_publ: bytes) -> bool:
    """Write one alias row to out.

    Args:
        out: A writeable file-like object.
        alias: The normalized alias byte-string.
        key: The original DOCDB key.
        date_publ: The publication date byte-string (may be empty).

    Returns:
        True on success.
    """
    out.write(alias + b"\t" + key + b"\t" + (date_publ or MISSING_DATE) + b"\n")
    return True


def emit(src, out) -> tuple[int, int, int, int, int, int, int, int]:
    """Read 6-col TSV rows from src and write 3-col alias input to out.

    Args:
        src: An iterable of byte-strings or text-strings (TSV rows).
        out: A writeable file-like object.

    Returns:
        A tuple of seven integers:
        (n_in, n_out, n_zero_stripped, n_wo_two_digit_year, n_jp_era,
        n_skipped_equal, n_skipped_pattern) so the caller can
        sanity check coverage.
    """
    n_in = 0
    n_out = 0
    n_zero_stripped = 0
    n_wo_two_digit_year = 0
    n_jp_era = 0
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
        alt = parts[2]
        date_publ = parts[4]
        if len(key) < 2:
            continue

        for synonym in zero_stripped_synonyms(key):
            if _write_alias(out, normalize_key(synonym[:2], synonym[2:]), key, date_publ):
                n_out += 1
                n_zero_stripped += 1

        for synonym in wo_two_digit_year_synonyms(key):
            if _write_alias(out, normalize_key(synonym[:2], synonym[2:]), key, date_publ):
                n_out += 1
                n_wo_two_digit_year += 1

        for synonym in jp_era_synonyms(key):
            if _write_alias(out, normalize_key(synonym[:2], synonym[2:]), key, date_publ):
                n_out += 1
                n_jp_era += 1

        alt_outcome = alt_alias(key, alt)
        if alt_outcome.skipped_equal:
            n_skipped_equal += 1
        if alt_outcome.skipped_pattern:
            n_skipped_pattern += 1
        if alt_outcome.alias is not None:
            if _write_alias(out, alt_outcome.alias, key, date_publ):
                n_out += 1

    return (
        n_in,
        n_out,
        n_zero_stripped,
        n_wo_two_digit_year,
        n_jp_era,
        n_skipped_equal,
        n_skipped_pattern,
    )
