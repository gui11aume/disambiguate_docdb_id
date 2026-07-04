#!/usr/bin/env python3
"""Project the main 6-column TSV down to layer_1's 2-column input.

The main TSV has columns:

    key \\t docdb_id \\t orig_doc_number \\t inventor \\t date_publ \\t family_id

For every row we form the candidate identifier `key[:2] + orig_doc_number`
— i.e. the row's country code prepended to the office's native
publication number — and re-normalise it with
`helpers.processed_doc_number`. The API endpoint will receive the
country code and the number as two separate parameters and combine
them in the same way, so layer_1 keys built here are byte-for-byte
identical to what the endpoint will compute at lookup time.

Each surviving row is emitted as ::

    processed(key[:2] + orig_doc_number) \\t key

We skip rows where:
  * `orig_doc_number` is empty or normalises to nothing (no usable
    alternative identifier);
  * the normalised alias does not match `[A-Z][A-Z][A-Z0-9][-A-Z0-9]*`;
  * the normalised alias collapses onto `key` itself (a direct probe
    of `docs_db` already resolves the query, so an indirection
    through layer_1 would be pure overhead and a likely source of
    duplicate-key noise during the bulk load).

The output is intentionally unsorted: the downstream loader requires
ascending order on column 1, so this script is meant to be piped into
`LC_ALL=C sort -u` before being fed to `initialize_layer1_from_tsv.py`.
`sort -u` removes lines that are byte-identical (same processed alias
*and* same primary key), which is the common case when the same row
appears in more than one back-file XML; genuine collisions — same
processed alias mapping to *different* primary keys — survive the
dedup, end up adjacent after sorting, and trip the loader's loud-fail
path.

Usage:
    extract_layer1_tsv.py [<sorted.tsv>]            # input defaults to stdin
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from helpers import processed_doc_number

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
LAYER_1_ALIAS_RE = re.compile(rb"^[A-Z][A-Z][A-Z0-9][A-Z0-9][A-Z0-9]+$")
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
    """Yield JP… aliases for a JPH/JPS key by dropping the era letter.

    JP DOCDB keys carry a Heisei (`H`) or Showa (`S`) era prefix
    that external systems frequently omit when quoting the same
    publication. We emit `JP` + suffix as the canonical-form alias,
    plus each progressively zero-stripped variant so callers can find
    the key whether or not they padded the document number. The
    3-digit lower bound on the suffix keeps every emitted alias at or
    above the 5-character minimum enforced by `LAYER_1_ALIAS_RE`.
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
    """Yield zero-padded JP… aliases for a JPH/JPS key.

    Canonical form is ``JP{H,S}YY<doc>`` with ``<doc>`` having leading
    zeros stripped (1-5 digits). External systems often quote the same
    publication with the doc number padded to a fixed 6-digit width.
    For each target width from ``len(doc)+1`` up to 6, emit both the
    era-kept and era-stripped variants. JP is the one CC where leading
    zeros after the country code are preserved by the lookup, so the
    era-stripped variants are emitted verbatim.
    """
    match = JP_ERA_PADDED_RE.fullmatch(key)
    if match is None:
        return

    era, yy, doc = match.groups()
    for target_len in range(len(doc) + 1, 7):
        padded = doc.rjust(target_len, b"0")
        yield b"JP" + era + yy + padded
        yield b"JP" + yy + padded


def _write_alias(out, alias: bytes, key: bytes) -> bool:
    """Normalize *alias* and write the layer_1 row. Returns True on success.

    Per the lookup contract, every CC except ``JP`` strips leading
    zeros from the number portion at query time, so a layer_1 alias of
    the form ``CC0…`` for those CCs is unreachable. We peel off leading
    zeros at position 2 until the next character is non-zero. The
    alias is dropped if the body collapses below the 3-character
    minimum required by ``LAYER_1_ALIAS_RE``.
    """
    if alias[:2] != b"JP":
        body = alias[2:].lstrip(b"0")
        if len(body) < 3:
            return False
        alias = alias[:2] + body
    out.write(alias + b"\t" + key + b"\n")
    return True


def emit(src, out) -> tuple[int, int, int, int, int, int, int, int]:
    """Read 6-col TSV rows from *src* and write 2-col layer_1 input to *out*.

    Returns `(n_in, n_out, n_zero_stripped, n_wo_two_digit_year,
    n_jp_era, n_jp_era_padded, n_skipped_equal, n_skipped_pattern)` so
    the caller can sanity check coverage.
    `n_zero_stripped` counts key aliases produced by removing leading
    zeros from the 6-digit suffix of a `CCYYYYNNNNNN` key.
    `n_wo_two_digit_year` counts `WOYYYY` aliases produced from
    historical `WOYYNNNNN` keys.
    `n_jp_era` counts `JP…` aliases produced from `JPH`/`JPS`
    keys (one per zero-strip variant, including the unstripped form).
    `n_jp_era_padded` counts zero-padded `JPH…`/`JPS…`/`JP…` aliases
    produced from `JP{H,S}YY<doc>` keys.
    `n_skipped_equal` counts rows where the processed alias collapsed
    onto the primary key and was therefore omitted; `n_skipped_pattern`
    counts rows whose processed alias did not match the layer_1 key
    pattern.
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
            print(f"warning: malformed line {n_in}: {line[:80]!r}", file=sys.stderr)
            continue

        key = parts[0]
        orig = parts[2]
        if len(key) < 2:
            continue

        cc = key[:2]
        for synonym in zero_stripped_key_synonyms(key):
            if _write_alias(out, synonym, key):
                n_out += 1
                n_zero_stripped += 1

        for synonym in wo_two_digit_year_synonyms(key):
            if _write_alias(out, synonym, key):
                n_out += 1
                n_wo_two_digit_year += 1

        for synonym in jp_era_synonyms(key):
            if _write_alias(out, synonym, key):
                n_out += 1
                n_jp_era += 1

        for synonym in jp_era_padded_synonyms(key):
            if _write_alias(out, synonym, key):
                n_out += 1
                n_jp_era_padded += 1

        if not orig:
            continue

        if key.endswith(orig.lstrip(b"0")):
            n_skipped_equal += 1
            continue

        alias = processed_doc_number(orig)
        if not alias:
            continue
        if alias == key:
            n_skipped_equal += 1
            continue
        if cc + alias == key:
            n_skipped_equal += 1
            continue

        country_rule = COUNTRY_RULES.get(cc)
        if country_rule is not None:
            patterns, trim_suffix = country_rule
            if any(pattern.fullmatch(alias) for pattern in patterns):
                country_alias = cc + (alias[:-trim_suffix] if trim_suffix else alias)
                if country_alias == key:
                    n_skipped_equal += 1
                    continue
                if _write_alias(out, country_alias, key):
                    n_out += 1
                continue

        if JUST_NUMBERS_RE.fullmatch(alias) and len(alias) > 3:
            alias = cc + alias
        if not LAYER_1_ALIAS_RE.fullmatch(alias):
            n_skipped_pattern += 1
            print(f"skipped pattern: {orig!r} {alias!r} ({key!r})", file=sys.stderr)
            continue

        if _write_alias(out, alias, key):
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("usage: extract_layer1_tsv.py [<sorted.tsv>]", file=sys.stderr)
        return 2

    out = sys.stdout.buffer
    if argv:
        src_path = Path(argv[0])
        with src_path.open("rb") as src:
            (
                n_in,
                n_out,
                n_zero_stripped,
                n_wo_two_digit_year,
                n_jp_era,
                n_jp_era_padded,
                n_skipped_equal,
                n_skipped_pattern,
            ) = emit(src, out)
    else:
        (
            n_in,
            n_out,
            n_zero_stripped,
            n_wo_two_digit_year,
            n_jp_era,
            n_jp_era_padded,
            n_skipped_equal,
            n_skipped_pattern,
        ) = emit(sys.stdin.buffer, out)

    print(
        f"layer_1 extract: {n_in:,} rows in, {n_out:,} aliases out, "
        f"{n_zero_stripped:,} zero-stripped key aliases, "
        f"{n_wo_two_digit_year:,} WO two-digit-year aliases, "
        f"{n_jp_era:,} JP era aliases, "
        f"{n_jp_era_padded:,} JP era padded aliases, "
        f"{n_skipped_equal:,} skipped (alias == key), "
        f"{n_skipped_pattern:,} skipped (alias pattern)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
