"""LMDB layout constants and shared record types."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TypeAlias

DEFAULT_MAP_SIZE = 100 * 1024**3  # 100 GiB; LMDB files are sparse.
DEFAULT_COMMIT_EVERY = 100_000

DOCS_DB_NAME = b"docs"
ALIAS_DB_NAME = b"alias"
META_DB_NAME = b"meta"
META_KEY_CORE_BUILD_STATUS = b"core_build_status"
META_KEY_CORE_LAST_UPDATED = b"core_last_updated"

META_KEY_ALIAS_BUILD_STATUS = b"alias_build_status"
META_KEY_ALIAS_LAST_UPDATED = b"alias_last_updated"

META_KEY_FRONTFILE_LAST_APPLIED = b"frontfile_last_applied"
FRONTFILE_APPLIED_PREFIX = b"frontfile_applied:"

BUILD_STATUS_IN_PROGRESS = b"in_progress"
BUILD_STATUS_COMPLETE = b"complete"

# frontfile `status` attribute values on `<exch:exchange-document>`.
STATUS_AMEND = "A"
STATUS_DELETE = "D"
STATUS_CREATE = "C"

Record: TypeAlias = list[str]


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
