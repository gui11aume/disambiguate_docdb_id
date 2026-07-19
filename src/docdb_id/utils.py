"""Shared helpers used across the package."""

from __future__ import annotations


def normalize_key(cc: bytes, doc_number: bytes) -> bytes:
    """Remove leading zeros from key, except for JP.

    The leading zeros are not removed for JP because documents of the Heisei era
    (e.g., JPH02012345) are often quoted as year - number (e.g., JP 02-012345). This
    would create potential ambiguity because the number is written with variable
    digit width (e.g., JP 02-012345 vs. JP 20-12345).

    Args:
        cc: Two-letter country code as bytes.
        doc_number: Document number as bytes.

    Returns:
        Upper-cased key bytes with leading zeros stripped from the
        numeric portion.
    """
    CC = cc.upper()
    if CC == b"JP":
        return CC + doc_number.upper()
    stripped = doc_number.upper().lstrip(b"0")
    return CC + stripped
