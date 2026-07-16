"""Shared streaming BDDS ingest engine: download -> expand -> parse -> cleanup.

Both backfile and frontfile deliveries share the same physical layout: an outer
delivery zip expands to `Root/DOC/*.zip`, and each inner zip holds exactly one
XML. A bounded queue of coordinator threads each owns one outer zip from fetch
through cleanup; a shared process pool parses inner XML into TSV parts.

Resume differs by product, reflecting what each can verify without downloading:

* Backfile: one TSV per inner XML (`part_<xml_stem>.tsv`) plus a per-outer-zip
  done marker under `<out-dir>/.outer/`. The marker is the skip signal.
* Frontfile: one TSV per outer delivery part (`part_<outer_stem>.tsv`),
  concatenating every inner XML of that part through a single parser target so
  the `seq` token stays continuous. The TSV itself is the skip signal - its
  presence means the whole part was ingested - so no marker is needed. The set
  of expected parts comes straight from the BDDS delivery metadata.
"""

from __future__ import annotations

import functools
import gzip
import logging
import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from lxml import etree as LET
from tqdm import tqdm

from docdb_id.bdds.client import (
    BddsClient,
    BddsError,
    credentials_from_env,
    file_is_complete,
    unlink_quietly,
)
from docdb_id.normalize import EntityNormalizingReader
from docdb_id.parse.docdb_target import BackfileTarget, FrontfileTarget

logger = logging.getLogger("docdb_id.bdds.ingest")

_CHUNK = 1 << 20  # 1 MiB

# OAuth token refresh is not lock-protected inside BddsClient; serialize downloads.
_DOWNLOAD_LOCK = threading.Lock()

_GZIP_MAGIC = b"\x1f\x8b"


def open_xml(path: Path) -> IO[bytes]:
    """Open a file as a binary stream, transparently decompressing gzip content.

    Args:
        path: Filesystem path to the XML file.

    Returns:
        A binary IO stream.
    """
    with path.open("rb") as f:
        magic = f.read(len(_GZIP_MAGIC))
    if magic == _GZIP_MAGIC:
        return gzip.open(path, "rb")
    return path.open("rb")


# ── Parse workers (module-level for pickling) ────────────────────────────────


def _parse_backfile_xml(job: tuple[str, str]) -> str | None:
    """Parse one backfile inner XML into a 6-column TSV part.

    Args:
        job: Tuple of (xml_path, part_output_path) as strings.

    Returns:
        str | None: Error message if parsing failed, None on success.
    """
    xml_path, part_path = Path(job[0]), Path(job[1])
    tmp_path = part_path.with_name(f"{part_path.name}.tmp")
    try:
        target = BackfileTarget()
        parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
        with xml_path.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                parser.feed(chunk)
        rows = parser.close()
        if not rows:
            logger.warning("parse_backfile_xml: %s produced 0 rows (lxml recover may have eaten errors)", xml_path.name)
        with tmp_path.open("wb") as out:
            for row in rows:
                out.write(row)
        tmp_path.replace(part_path)
        return None
    except Exception as exc:
        unlink_quietly(tmp_path)
        return f"{xml_path.name}: {exc}"


def _parse_frontfile_part(job: tuple[list[str], str, int]) -> str | None:
    """Parse all inner XMLs of one delivery part into a single changelog TSV.

    Every inner XML is fed through *one* `FrontfileTarget` so the per-document
    position counter behind the `seq` token runs continuously across the whole
    part; `file_idx` (the part's global chronological index) is the high half
    of that token. Written via a sibling `*.tmp` and atomically renamed, so an
    interrupted run never leaves a partial part that resume would treat as done.

    Args:
        job: Tuple of (xml_path_strings, part_output_path, file_idx).

    Returns:
        str | None: Error message if parsing failed, None on success.
    """
    xml_strs, part_path_str, file_idx = job
    part_path = Path(part_path_str)
    tmp_path = part_path.with_name(f"{part_path.name}.tmp")
    try:
        target = FrontfileTarget(file_idx)
        for xml_str in xml_strs:
            parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
            with open_xml(Path(xml_str)) as raw:
                reader = EntityNormalizingReader(raw)
                while True:
                    chunk = reader.read(_CHUNK)
                    if not chunk:
                        break
                    parser.feed(chunk)
            parser.close()
        if not target.rows:
            logger.warning("parse_frontfile_part: %s produced 0 rows (lxml recover may have eaten errors)", part_path.name)
        with tmp_path.open("wb") as out:
            for row in target.rows:
                out.write(row)
        tmp_path.replace(part_path)
        return None
    except Exception as exc:
        unlink_quietly(tmp_path)
        return f"{part_path.name}: {exc}"


# ── Container expansion ──────────────────────────────────────────────────────


def expand_nested_zip(container_path: Path, work_dir: Path) -> tuple[list[Path], list[str]]:
    """Outer zip -> `Root/DOC/*.zip` -> one XML each.

    Shared by both products: backfile and frontfile deliveries have the same
    nested layout.

    Args:
        container_path: Path to the outer delivery zip.
        work_dir: Temporary directory for extraction.

    Returns:
        tuple[list[Path], list[str]]: Tuple of (xml_paths, error_messages).
    """
    errors: list[str] = []
    try:
        with zipfile.ZipFile(container_path) as zf:
            zf.extractall(work_dir)
    except (zipfile.BadZipFile, FileNotFoundError, PermissionError) as exc:
        return [], [f"expand {container_path.name}: {exc}"]

    xml_dir = work_dir / "_xml"
    xml_dir.mkdir()
    xml_paths: list[Path] = []
    for inner_zip in sorted(work_dir.rglob("*.zip")):
        try:
            with zipfile.ZipFile(inner_zip) as zf:
                for member in zf.namelist():
                    if not member.lower().endswith(".xml"):
                        continue
                    target = xml_dir / Path(member).name
                    with zf.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=_CHUNK)
                    xml_paths.append(target)
        except (zipfile.BadZipFile, PermissionError) as exc:
            errors.append(f"{inner_zip.name}: {exc}")
        finally:
            unlink_quietly(inner_zip)
    return xml_paths, errors


# ── Work items and results ───────────────────────────────────────────────────


@dataclass
class WorkItem:
    """One outer delivery zip to fetch, expand, and parse.

    `key` is the outer zip stem, used to name the frontfile part TSV and the
    backfile done marker. `file_idx` is the part's global chronological index
    (frontfile only): the high half of the `seq` token.
    """

    key: str
    file_name: str
    local_path: Path
    file_id: int | None = None
    delivery_id: int | None = None
    file_idx: int = 0
    parser_kind: Literal["backfile", "frontfile"] = "backfile"
    file_meta: dict[str, Any] | None = None


@dataclass
class WorkResult:
    key: str
    skipped: bool = False
    n_xml: int = 0
    n_parsed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class IngestConfig:
    """Parameters for a single ingest run."""

    product_id: int
    delivery_mode: Literal["latest", "all"]
    parser_kind: Literal["backfile", "frontfile"]
    delete_after_parse: bool
    done_subdir: str  # backfile only: ".outer" marker dir (frontfile uses TSVs)
    out_dir: Path
    work_root: Path
    workers: int
    in_flight: int
    staging: Path | None = None
    offline_inputs: list[Path] | None = None
    skip_download_if_complete: bool = False  # frontfile: reuse valid staging files
    # frontfile: part stems already recorded as applied in the target LMDB's
    # meta sub-DB. Checked in addition to local TSV presence, so a cleared
    # staging directory does not force re-fetching the entire delivery history.
    applied_parts: frozenset[str] = frozenset()


# ── Coordinator ──────────────────────────────────────────────────────────────


def _done_marker(out_dir: Path, subdir: str, key: str) -> Path:
    """Return the path to the done marker for a backfile outer zip.

    Args:
        out_dir: Base output directory.
        subdir: Subdirectory under `out_dir` for markers.
        key: Outer zip stem used as the marker name.

    Returns:
        Path: The done marker file path.
    """
    return out_dir / subdir / f"{key}.done"


def _download_if_needed(
    client: BddsClient | None,
    product_id: int,
    item: WorkItem,
    *,
    skip_if_complete: bool,
) -> str | None:
    """Download `item.local_path` when online.

    Offline items (no client / file_id) are assumed already present on disk.

    Args:
        client: BDDS client instance, or None for offline mode.
        product_id: The product identifier.
        item: The work item to download.
        skip_if_complete: Skip download if the file is already complete on disk.

    Returns:
        str | None: Error message if download failed, None on success.
    """
    if client is None or item.file_id is None or item.delivery_id is None:
        return None
    if skip_if_complete and item.file_meta is not None and file_is_complete(item.local_path, item.file_meta):
        return None
    try:
        with _DOWNLOAD_LOCK:
            client.download_file(product_id, item.delivery_id, item.file_id, item.local_path)
    except BddsError as exc:
        return f"download {item.file_name}: {exc}"
    return None


def _handle_item(
    item: WorkItem,
    *,
    client: BddsClient | None,
    product_id: int,
    out_dir: Path,
    work_root: Path,
    parse_pool: ProcessPoolExecutor,
    delete_source: bool,
    done_subdir: str,
    skip_download_if_complete: bool,
) -> WorkResult:
    """Dispatch one outer zip to the product-specific handler.

    Args:
        item: The work item to process.
        client: BDDS client instance, or None for offline mode.
        product_id: The product identifier.
        out_dir: Output directory for parsed TSVs.
        work_root: Temporary working directory root.
        parse_pool: Shared process pool for XML parsing.
        delete_source: Whether to delete the source zip after parsing.
        done_subdir: Subdirectory for backfile done markers.
        skip_download_if_complete: Skip download if the file is complete.

    Returns:
        WorkResult: Outcome of processing.
    """
    if item.parser_kind == "frontfile":
        return _handle_frontfile_item(
            item,
            client=client,
            product_id=product_id,
            out_dir=out_dir,
            work_root=work_root,
            parse_pool=parse_pool,
            delete_source=delete_source,
            skip_download_if_complete=skip_download_if_complete,
        )
    return _handle_backfile_item(
        item,
        client=client,
        product_id=product_id,
        out_dir=out_dir,
        work_root=work_root,
        parse_pool=parse_pool,
        delete_source=delete_source,
        done_subdir=done_subdir,
    )


def _handle_frontfile_item(
    item: WorkItem,
    *,
    client: BddsClient | None,
    product_id: int,
    out_dir: Path,
    work_root: Path,
    parse_pool: ProcessPoolExecutor,
    delete_source: bool,
    skip_download_if_complete: bool,
) -> WorkResult:
    """One outer delivery part -> one `part_<stem>.tsv`; the TSV is the resume key.

    Args:
        item: The work item to process.
        client: BDDS client instance, or None for offline mode.
        product_id: The product identifier.
        out_dir: Output directory for parsed TSVs.
        work_root: Temporary working directory root.
        parse_pool: Shared process pool for XML parsing.
        delete_source: Whether to delete the source zip after parsing.
        skip_download_if_complete: Skip download if the file is complete.

    Returns:
        WorkResult: Outcome of processing.
    """
    part_path = _frontfile_part_path(out_dir, item.key)
    if part_path.exists():
        return WorkResult(item.key, skipped=True)

    err = _download_if_needed(client, product_id, item, skip_if_complete=skip_download_if_complete)
    if err:
        return WorkResult(item.key, errors=[err])
    if not item.local_path.exists():
        return WorkResult(item.key, errors=[f"missing container: {item.local_path}"])

    work = Path(tempfile.mkdtemp(prefix=f"{item.key}_", dir=work_root))
    try:
        xml_paths, errors = expand_nested_zip(item.local_path, work)
        # Any expansion failure leaves the part TSV unwritten so a later run
        # retries the whole part (presence of the TSV must mean "fully done").
        if errors:
            return WorkResult(item.key, n_xml=len(xml_paths), errors=errors)

        xml_strs = [str(p) for p in sorted(xml_paths, key=lambda p: p.name)]
        job = (xml_strs, str(part_path), item.file_idx)
        parse_err = parse_pool.submit(_parse_frontfile_part, job).result()
        if parse_err:
            return WorkResult(item.key, n_xml=len(xml_paths), errors=[parse_err])
        return WorkResult(item.key, n_xml=len(xml_paths), n_parsed=len(xml_paths))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if delete_source:
            unlink_quietly(item.local_path)


def _handle_backfile_item(
    item: WorkItem,
    *,
    client: BddsClient | None,
    product_id: int,
    out_dir: Path,
    work_root: Path,
    parse_pool: ProcessPoolExecutor,
    delete_source: bool,
    done_subdir: str,
) -> WorkResult:
    """One outer zip -> one `part_<xml_stem>.tsv` per inner XML; marker is the resume key.

    Args:
        item: The work item to process.
        client: BDDS client instance, or None for offline mode.
        product_id: The product identifier.
        out_dir: Output directory for parsed TSVs.
        work_root: Temporary working directory root.
        parse_pool: Shared process pool for XML parsing.
        delete_source: Whether to delete the source zip after parsing.
        done_subdir: Subdirectory for done markers.

    Returns:
        WorkResult: Outcome of processing.
    """
    marker = _done_marker(out_dir, done_subdir, item.key)
    if marker.exists():
        return WorkResult(item.key, skipped=True)

    err = _download_if_needed(client, product_id, item, skip_if_complete=False)
    if err:
        return WorkResult(item.key, errors=[err])
    if not item.local_path.exists():
        return WorkResult(item.key, errors=[f"missing container: {item.local_path}"])

    work = Path(tempfile.mkdtemp(prefix=f"{item.key}_", dir=work_root))
    errors: list[str] = []
    try:
        xml_paths, expand_errors = expand_nested_zip(item.local_path, work)
        errors.extend(expand_errors)

        jobs: list[tuple[str, str]] = []
        for xml_path in sorted(xml_paths, key=lambda p: p.name):
            part = out_dir / f"part_{xml_path.stem}.tsv"
            if part.exists():
                continue
            jobs.append((str(xml_path), str(part)))

        for future in [parse_pool.submit(_parse_backfile_xml, job) for job in jobs]:
            parse_err = future.result()
            if parse_err:
                errors.append(parse_err)

        if not errors:
            marker.touch()
        return WorkResult(item.key, n_xml=len(xml_paths), n_parsed=len(jobs), errors=errors)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if delete_source:
            unlink_quietly(item.local_path)


# ── Enumeration ──────────────────────────────────────────────────────────────


def _collect_local_zips(inputs: list[Path]) -> list[Path]:
    """Resolve offline inputs to a flat, sorted list of outer `.zip` files.

    Directories are scanned non-recursively so the glob does not descend into
    any expanded `Root/DOC/*.zip` trees left from a previous run.

    Args:
        inputs: List of file or directory paths to scan.

    Returns:
        list[Path]: Sorted list of zip file paths.
    """
    zips: list[Path] = []
    for path in inputs:
        if path.is_dir():
            zips.extend(sorted(path.glob("*.zip")))
        elif path.is_file() and path.suffix.lower() == ".zip":
            zips.append(path)
        else:
            logger.warning("skipping unsupported input: %s", path)
    return zips


def _build_online_items(
    client: BddsClient,
    config: IngestConfig,
    staging: Path,
) -> list[WorkItem]:
    """Build work items from BDDS delivery metadata.

    Enumerates deliveries for the configured product and creates a WorkItem
    for each outer delivery zip. Frontfile items are assigned a global
    chronological index via `file_idx`.

    Args:
        client: BDDS client instance.
        config: Ingest configuration.
        staging: Local staging directory for downloaded files.

    Returns:
        list[WorkItem]: Ordered list of work items to process.
    """
    staging.mkdir(parents=True, exist_ok=True)
    items: list[WorkItem] = []
    file_idx = 0

    if config.delivery_mode == "latest":
        delivery = client.latest_delivery(config.product_id)
        deliveries = [delivery]
    else:
        deliveries = client.all_deliveries(config.product_id)

    # Each outer delivery zip becomes one WorkItem. For the frontfile, the
    # position in this (oldest-delivery-first, then fileName) walk is the part's
    # global chronological index, folded into the seq token; it stays stable
    # across runs because new deliveries are only ever appended.
    for delivery in deliveries:
        delivery_id = delivery["deliveryId"]
        files = sorted(delivery.get("files") or [], key=lambda x: x.get("fileName", ""))
        for f in files:
            name = str(f.get("fileName", ""))
            if not name.lower().endswith(".zip"):
                continue
            items.append(
                WorkItem(
                    key=Path(name).stem,
                    file_name=name,
                    local_path=staging / name,
                    file_id=f["fileId"],
                    delivery_id=delivery_id,
                    file_idx=file_idx,
                    parser_kind=config.parser_kind,
                    file_meta=f,
                )
            )
            file_idx += 1

    if config.delivery_mode == "latest":
        logger.info(
            "latest delivery %s (%s): %d file(s)",
            deliveries[0]["deliveryId"],
            deliveries[0].get("deliveryName", ""),
            len(items),
        )
    elif config.parser_kind != "frontfile":
        logger.info("%d part(s) across %d delivery(s)", len(items), len(deliveries))

    return items


def _build_offline_items(inputs: list[Path], config: IngestConfig) -> list[WorkItem]:
    """Build work items from local zip files for offline processing.

    Args:
        inputs: List of local file or directory paths.
        config: Ingest configuration.

    Returns:
        list[WorkItem]: Ordered list of work items.
    """
    paths = sorted(_collect_local_zips(inputs), key=lambda p: p.name)
    return [
        WorkItem(
            key=path.stem,
            file_name=path.name,
            local_path=path,
            file_idx=idx,
            parser_kind=config.parser_kind,
        )
        for idx, path in enumerate(paths)
    ]


def _frontfile_part_path(out_dir: Path, key: str) -> Path:
    """Return the path to the frontfile part TSV.

    Args:
        out_dir: Output directory.
        key: Outer zip stem.

    Returns:
        Path: Path to the part TSV file.
    """
    return out_dir / f"part_{key}.tsv"


def _partition_frontfile_items(
    items: list[WorkItem], out_dir: Path, applied_parts: frozenset[str] = frozenset()
) -> tuple[list[WorkItem], int]:
    """Split catalog items into pending work and a count of already-ingested parts.

    An item counts as already ingested if its part TSV is already staged
    locally, or if its key is already recorded as applied in the target LMDB
    (`applied_parts`) - the latter is what lets local staging be deleted
    between runs without forcing a re-fetch of the whole delivery history.

    Args:
        items: All catalog work items.
        out_dir: Output directory for part TSVs.
        applied_parts: Frozenset of part stems already applied to the target LMDB.

    Returns:
        tuple[list[WorkItem], int]: (pending work items, count already ingested).
    """
    pending = [
        item
        for item in items
        if item.key not in applied_parts and not _frontfile_part_path(out_dir, item.key).exists()
    ]
    return pending, len(items) - len(pending)


# ── Public entry point ───────────────────────────────────────────────────────


def run(config: IngestConfig) -> int:
    """Execute a streaming ingest run. Returns 0 on success, 1 on errors."""
    config.out_dir.mkdir(parents=True, exist_ok=True)
    # Frontfile resume keys off the part TSVs themselves; only the backfile
    # keeps per-outer-zip done markers under done_subdir.
    if config.parser_kind == "backfile":
        (config.out_dir / config.done_subdir).mkdir(exist_ok=True)
    config.work_root.mkdir(parents=True, exist_ok=True)

    client: BddsClient | None = None
    online = config.offline_inputs is None

    if online:
        username, password = credentials_from_env()
        client = BddsClient(username, password)
        staging = config.staging or (config.out_dir.parent / "bdds_staging")
        items = _build_online_items(client, config, staging)
    else:
        items = _build_offline_items(config.offline_inputs, config)

    if not items:
        logger.error("no containers to process")
        return 1

    label = "backfile" if config.parser_kind == "backfile" else "frontfile"
    work_items = items
    skipped_upfront = 0
    if config.parser_kind == "frontfile":
        work_items, skipped_upfront = _partition_frontfile_items(items, config.out_dir, config.applied_parts)
        if online and config.delivery_mode == "all":
            n_deliveries = len({item.delivery_id for item in items if item.delivery_id is not None})
            logger.info(
                "catalog: %d part(s) across %d delivery(s); %d already ingested, %d pending",
                len(items),
                n_deliveries,
                skipped_upfront,
                len(work_items),
            )
        else:
            logger.info(
                "catalog: %d part(s); %d already ingested, %d pending",
                len(items),
                skipped_upfront,
                len(work_items),
            )
        if not work_items:
            logger.info("nothing to do")
            return 0

    if config.parser_kind == "frontfile" and skipped_upfront:
        logger.info(
            "ingesting %d pending part(s) (%d already ingested): %d parse worker(s), %d in flight -> %s",
            len(work_items),
            skipped_upfront,
            config.workers,
            config.in_flight,
            config.out_dir,
        )
    else:
        logger.info(
            "ingesting %d container(s): %d parse worker(s), %d in flight -> %s",
            len(work_items),
            config.workers,
            config.in_flight,
            config.out_dir,
        )

    delete_source = online and config.delete_after_parse
    handle = functools.partial(
        _handle_item,
        client=client,
        product_id=config.product_id,
        out_dir=config.out_dir,
        work_root=config.work_root,
        delete_source=delete_source,
        done_subdir=config.done_subdir,
        skip_download_if_complete=config.skip_download_if_complete,
    )

    errors = 0
    processed = 0
    skipped = 0
    with ProcessPoolExecutor(max_workers=config.workers) as parse_pool:
        bound = functools.partial(handle, parse_pool=parse_pool)
        with ThreadPoolExecutor(max_workers=config.in_flight) as coordinators:
            for result in tqdm(
                coordinators.map(bound, work_items),
                total=len(work_items),
                unit="zip",
                desc=label,
            ):
                if result.skipped:
                    skipped += 1
                    continue
                processed += 1
                for err in result.errors:
                    logger.warning("warning: %s", err)
                    errors += 1

    skipped += skipped_upfront
    logger.info("done: %d processed, %d skipped, %d error(s)", processed, skipped, errors)
    return 0 if errors == 0 else 1
