"""Minimal client for the EPO Bulk Data Distribution Service.

Wraps the endpoints used to enumerate and download DOCDB deliveries, ported to
Python with no extra runtime dependency beyond `tqdm` (already a project dep):

    POST   https://login.epo.org/oauth2/.../v1/token            (OAuth2 password)
    GET    .../api/products/                                    (list products)
    GET    .../api/products/{productId}                         (deliveries)
    GET    .../api/products/{productId}/delivery/{deliveryId}/file/{fileId}/download

Authentication uses OAuth2 password grant against a fixed EPO client ID. The
returned bearer token is valid for 1 hour and is refreshed automatically.

The EPO download endpoint does **not** honour HTTP `Range` requests: it
ignores the header and returns the full body with status 200. We therefore
never attempt to resume - every file is fetched from byte 0 on every run.

Credentials must be supplied via environment variables:

    export EPO_BDDS_USERNAME=...
    export EPO_BDDS_PASSWORD=...
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

logger = logging.getLogger("docdb_id.bdds.client")

# ── Endpoints and the EPO-specific OAuth client ──────────────────────────────

OAUTH_URL = "https://login.epo.org/oauth2/aus3up3nz0N133c0V417/v1/token"
# Base64 of the EPO OAuth client ID, used in a Basic auth header on the token
# request. There is no client secret; this is the same hardcoded value the
# upstream Go client (patent-dev/epo-bdds) ships with.
OAUTH_CLIENT_ID_B64 = "MG9hM3VwZG43YW41cE1JOE80MTc="
API_BASE = "https://publication-bdds.apps.epo.org/bdds/bdds-bff-service/prod/api"

USER_AGENT = "disambiguate-docdb-id/0.1"
TOKEN_TTL = timedelta(hours=1)
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)
DEFAULT_TIMEOUT = 60  # seconds, for control-plane (JSON) requests
DOWNLOAD_TIMEOUT = 300  # seconds, for body reads on downloads
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2.0  # seconds; multiplied by 2**attempt
DOWNLOAD_CHUNK = 1 << 20  # 1 MiB
PART_SUFFIX = ".part"  # in-progress downloads land here until fully written


class BddsError(RuntimeError):
    """Raised on any failure talking to the EPO BDDS API."""


class BddsAuthError(BddsError):
    """OAuth2 token request failed."""


# ── HTTP client ──────────────────────────────────────────────────────────────


class BddsClient:
    """Minimal EPO BDDS client.

    The token is cached in-process and refreshed when it gets within
    `TOKEN_REFRESH_BUFFER` of expiry, or after a 401 from the server.
    """

    def __init__(self, username: str, password: str, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token: str | None = None
        self._token_expiry: datetime = datetime.min.replace(tzinfo=timezone.utc)

    # ---- authentication ------------------------------------------------------

    def _authenticate(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": "openid",
            }
        ).encode("ascii")
        req = urllib.request.Request(
            OAUTH_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {OAUTH_CLIENT_ID_B64}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BddsAuthError(f"OAuth token request failed ({exc.code}): {_safe_error_body(exc)}") from exc
        except urllib.error.URLError as exc:
            raise BddsAuthError(f"OAuth token request failed: {exc.reason}") from exc

        try:
            self._token = payload["access_token"]
        except KeyError as exc:
            raise BddsAuthError(f"OAuth response missing access_token: {payload!r}") from exc
        # The response includes `expires_in` (seconds); we ignore it in favour
        # of the documented 1h TTL minus a safety buffer.
        self._token_expiry = datetime.now(timezone.utc) + TOKEN_TTL
        logger.debug("acquired EPO BDDS token, expires at %s", self._token_expiry.isoformat())

    def _ensure_token(self) -> str:
        if self._token is None or datetime.now(timezone.utc) + TOKEN_REFRESH_BUFFER >= self._token_expiry:
            self._authenticate()
        assert self._token is not None
        return self._token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = datetime.min.replace(tzinfo=timezone.utc)

    # ---- low-level requests --------------------------------------------------

    def _build_request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._ensure_token()}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )

    def _get_json(self, path: str) -> Any:
        url = f"{API_BASE}{path}"
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            req = self._build_request(url)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_exc = exc
                # Re-auth once on 401: a server-side token revocation can land
                # before our local expiry catches up.
                if exc.code == 401 and attempt == 0:
                    self._invalidate_token()
                    continue
                if exc.code == 404:
                    raise BddsError(f"GET {url}: 404 not found") from exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    _sleep_backoff(attempt, reason=f"HTTP {exc.code}")
                    continue
                raise BddsError(f"GET {url}: HTTP {exc.code}: {_safe_error_body(exc)}") from exc
            except urllib.error.URLError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    _sleep_backoff(attempt, reason=str(exc.reason))
                    continue
                raise BddsError(f"GET {url} failed: {exc.reason}") from exc
        raise BddsError(f"GET {url} failed after {MAX_RETRIES} attempts: {last_exc}")

    # ---- high-level API ------------------------------------------------------

    def list_products(self) -> list[dict[str, Any]]:
        return self._get_json("/products/")

    def get_product(self, product_id: int) -> dict[str, Any]:
        return self._get_json(f"/products/{product_id}")

    def latest_delivery(self, product_id: int) -> dict[str, Any]:
        deliveries = self.get_product(product_id).get("deliveries") or []
        if not deliveries:
            raise BddsError(f"product {product_id} has no deliveries")
        # The publication datetime is an ISO 8601 string; lexicographic order
        # matches chronological order for that format.
        return max(deliveries, key=lambda d: d.get("deliveryPublicationDatetime", ""))

    def all_deliveries(self, product_id: int) -> list[dict[str, Any]]:
        deliveries = self.get_product(product_id).get("deliveries") or []
        if not deliveries:
            raise BddsError(f"product {product_id} has no deliveries")
        # Oldest first so incremental frontfile updates can be applied in order.
        return sorted(deliveries, key=lambda d: d.get("deliveryPublicationDatetime", ""))

    def download_file(self, product_id: int, delivery_id: int, file_id: int, dst: Path) -> None:
        """Stream one delivery file to `dst`. Always starts from byte 0.

        The EPO download endpoint ignores `Range` requests, so resuming a
        partially-downloaded file is not possible - every invocation pulls the
        full body and truncates the target.

        The body is streamed to a sibling `*.part` file and renamed onto
        `dst` only after the full transfer succeeds, so an interrupted
        download never leaves a truncated file at the final path.
        """
        url = f"{API_BASE}/products/{product_id}/delivery/{delivery_id}/file/{file_id}/download"
        part = dst.with_name(dst.name + PART_SUFFIX)
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            req = self._build_request(url)
            try:
                with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                    content_length = resp.headers.get("Content-Length")
                    total_bytes = int(content_length) if content_length is not None else None
                    _stream_to_file(resp, part, total_bytes=total_bytes)
                part.replace(dst)
                return
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code == 401 and attempt == 0:
                    self._invalidate_token()
                    continue
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    _sleep_backoff(attempt, reason=f"HTTP {exc.code}")
                    continue
                raise BddsError(f"download {dst.name}: HTTP {exc.code}: {_safe_error_body(exc)}") from exc
            except urllib.error.URLError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    _sleep_backoff(attempt, reason=str(exc.reason))
                    continue
                raise BddsError(f"download {dst.name} failed: {exc.reason}") from exc
        unlink_quietly(part)
        raise BddsError(f"download {dst.name} failed after {MAX_RETRIES} attempts: {last_exc}")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    """Best-effort decode of an HTTPError body for logging."""
    try:
        return exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        return "<no body>"


def _sleep_backoff(attempt: int, *, reason: str) -> None:
    delay = RETRY_BACKOFF_BASE * (2**attempt)
    logger.warning("retrying after %.1fs (attempt %d): %s", delay, attempt + 1, reason)
    time.sleep(delay)


def _stream_to_file(resp: Any, dst: Path, *, total_bytes: int | None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    progress = tqdm(
        total=total_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=dst.name,
        leave=False,
    )
    try:
        with dst.open("wb") as out:
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                progress.update(len(chunk))
    finally:
        progress.close()


def _expected_file_size(file_meta: dict[str, Any]) -> int | None:
    """Best-effort extraction of the advertised file size from a BDDS file object."""
    for key in ("fileSize", "fileSizeBytes", "size", "sizeInBytes", "contentLength"):
        value = file_meta.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.debug("ignoring non-integer %s for %s: %r", key, file_meta.get("fileName", "<unknown>"), value)
    return None


def unlink_quietly(path: Path) -> None:
    """Remove `path` if present, swallowing missing-file and OS errors."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("could not remove %s: %s", path, exc)


def _zip_is_valid(path: Path) -> bool:
    """Return True if `path` is a structurally sound zip archive.

    `testzip()` walks the central directory and CRC-checks every member, so a
    truncated download (missing the end-of-central-directory record or with a
    short final member) is reliably detected.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug("zip validation failed for %s: %s", path, exc)
        return False


def file_is_complete(dst: Path, file_meta: dict[str, Any]) -> bool:
    """Return True if `dst` already holds the fully-downloaded delivery file."""
    if not dst.exists():
        return False
    expected_size = _expected_file_size(file_meta)
    if expected_size is not None and dst.stat().st_size != expected_size:
        return False
    # Zip archives are the deliverables we must apply, so verify their integrity
    # rather than trusting mere existence: a truncated archive is re-fetched.
    if dst.suffix.lower() == ".zip":
        return _zip_is_valid(dst)
    return True


def credentials_from_env() -> tuple[str, str]:
    """Read EPO_BDDS_USERNAME and EPO_BDDS_PASSWORD from the environment.

    Exits the program with a clear error message if either is missing.
    """
    username = os.environ.get("EPO_BDDS_USERNAME", "")
    password = os.environ.get("EPO_BDDS_PASSWORD", "")
    if not username or not password:
        raise SystemExit(
            "EPO_BDDS_USERNAME and EPO_BDDS_PASSWORD must be set in the environment. "
            "Obtain credentials at https://www.epo.org/en/searching-for-patents/data/bulk-data-sets"
        )
    try:
        password.encode("ascii")
    except UnicodeEncodeError:
        logger.warning("EPO_BDDS_PASSWORD contains non-ASCII characters; authentication may fail")
    return username, password
