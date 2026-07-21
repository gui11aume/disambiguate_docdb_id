"""LMDB layout constants and shared record types."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

DEFAULT_MAP_SIZE = 100 * 1024**3  # 100 GiB; for cold backfile load only.
# Extra room beyond the current data.mdb for in-place frontfile / alias writes.
# Keeps VPS updates from re-opening at DEFAULT_MAP_SIZE and re-inflating the file.
DEFAULT_MAP_HEADROOM = 4 * 1024**3  # 4 GiB
DEFAULT_COMMIT_EVERY = 100_000

DOCS_DB_NAME = b"docs"
ALIAS_DB_NAME = b"alias"
META_DB_NAME = b"meta"
META_KEY_CORE_BUILD_STATUS = b"core_build_status"
META_KEY_CORE_LAST_UPDATED = b"core_last_updated"

META_KEY_ALIAS_BUILD_STATUS = b"alias_build_status"
META_KEY_ALIAS_LAST_UPDATED = b"alias_last_updated"

# Set (to the verification timestamp) when a prune pass has confirmed that every
# alias points to an existing `docs` key. Deleted by any step that can introduce
# dangling aliases (e.g. a frontfile apply), so its presence means "verified
# clean" and its absence means "unknown / possibly dangling".
META_KEY_ALIAS_NO_DANGLING = b"alias_no_dangling"

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
    """Return the current UTC time as an ISO 8601 string with second precision.

    Returns:
        ISO 8601 timestamp string (e.g. "2024-01-15T14:30:00+00:00").
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_map_size(lmdb_path: Path, *, headroom: int = DEFAULT_MAP_HEADROOM) -> int:
    """Return a map_size for in-place writes: current data.mdb size plus headroom.

    Args:
        lmdb_path: LMDB environment directory (or data.mdb file path).
        headroom: Bytes of growth room beyond the current file size.

    Returns:
        Map size in bytes suitable for opening the env for writes.
    """
    data = lmdb_path / "data.mdb" if lmdb_path.is_dir() else lmdb_path
    return data.stat().st_size + headroom
