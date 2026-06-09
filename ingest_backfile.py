#!/usr/bin/env python3
"""Streaming DOCDB backfile ingest: download → expand → parse → cleanup.

The EPO DOCDB backfile (BDDS product 14) is a terabyte-scale snapshot delivered
as thousands of "outer" zip files. Each outer zip expands to a small tree:

    Root/
      CONTENTS
      index.xml
      VOLUMEID
      DOC/
        DOCDB-<delivery>-<vol>-<seg>-NNNN.zip   ← one "inner" zip each
        ...

and each inner zip expands to a single `DOCDB-…-NNNN.xml`.

Downloading and unpacking the whole product at once would need terabytes of free
disk. This script instead processes one outer zip at a time as a bounded queue:
fetch the outer zip, expand it, parse every inner XML into a per-XML TSV part,
then delete the zip and the expanded tree before moving on. Only `--in-flight`
outer zips are ever materialized on disk simultaneously, so the steady-state
footprint is a small multiple of one delivery file plus the (small) growing
directory of TSV parts.

Two pools cooperate so downloads overlap parsing:

  * a thread pool of `--in-flight` coordinators, each owning one outer zip from
    fetch through cleanup (this is what bounds peak disk use), and
  * a shared process pool of `--workers` parsers that turn inner XML into TSV
    (XML parsing is CPU-bound, so it must run in separate processes).

Resume is automatic. Each outer zip gets a sentinel under `<out-dir>/.outer/`
once all its parts are written; on a re-run, completed outers are skipped (no
re-download) and any individually completed inner parts are skipped too.

Output is one `part_<inner-xml-stem>.tsv` per inner XML, byte-for-byte the same
columns as `backfile_to_tsv.py` (it reuses that module's parser), ready for the
merge-sort + LMDB-load stages.

Usage:
    # online: enumerate + download the latest backfile delivery, then parse
    ingest_backfile.py --download --out-dir DIR [--staging DIR] [--work-dir DIR]
                       [--workers N] [--in-flight N]

    # offline: parse a local directory of already-downloaded outer .zip files
    # (the source zips are treated as read-only and never deleted)
    ingest_backfile.py <dir-or-zip>... --out-dir DIR [--work-dir DIR]
                       [--workers N] [--in-flight N]

Online mode needs EPO_BDDS_USERNAME / EPO_BDDS_PASSWORD in the environment.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree as LET
from tqdm import tqdm

from backfile_to_tsv import _Target
from bdds_client import BddsClient, BddsError, credentials_from_env

logger = logging.getLogger("ingest_backfile")

BACKFILE_PRODUCT_ID = 14
_CHUNK = 1 << 20  # 1 MiB, for both XML copy-out and the parser feed loop.

# Downloads share one BddsClient whose OAuth token state is not lock-protected;
# serializing the actual HTTP downloads avoids a token-refresh race while still
# letting each coordinator parse (in separate processes) concurrently.
_DOWNLOAD_LOCK = threading.Lock()


# ── Inner-XML parse worker (runs in the process pool) ─────────────────────────


def _parse_xml_to_part(job: tuple[str, str]) -> str | None:
    """Parse one inner XML file into a TSV part. Returns an error string or None.

    Written to a sibling `*.tmp` and atomically renamed onto the final part
    path, so an interrupted parse never leaves a truncated part that a later
    resume would mistake for completed work. An XML that yields no rows still
    produces an (empty) part so the resume check treats it as done.

    Must be a module-level function so the process pool can pickle it.
    """
    xml_path, part_path = Path(job[0]), Path(job[1])
    tmp_path = part_path.with_name(part_path.name + ".tmp")
    try:
        target = _Target()
        parser = LET.XMLParser(target=target, recover=True, huge_tree=True, resolve_entities=False)
        with xml_path.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                parser.feed(chunk)
        rows = parser.close()
        with tmp_path.open("wb") as out:
            for row in rows:
                out.write(row)
        os.replace(tmp_path, part_path)
        return None
    except Exception as exc:
        _unlink_quietly(tmp_path)
        return f"{xml_path.name}: {exc}"


# ── Per-outer-zip coordinator (runs in the thread pool) ───────────────────────


@dataclass
class _Outer:
    """One unit of work: an outer backfile zip to fetch (online) or read
    (offline). `file_id` is set only in online mode; `local_path` is where the
    zip lives or will be downloaded to."""

    file_name: str
    local_path: Path
    file_id: int | None = None


@dataclass
class _OuterResult:
    stem: str
    skipped: bool = False
    n_xml: int = 0
    n_parsed: int = 0
    errors: list[str] = field(default_factory=list)


def _handle_outer(
    outer: _Outer,
    *,
    client: BddsClient | None,
    product_id: int,
    delivery_id: int | None,
    out_dir: Path,
    work_root: Path,
    parse_pool: ProcessPoolExecutor,
    delete_source: bool,
) -> _OuterResult:
    """Fetch (if online), expand, parse, and clean up one outer zip.

    The outer zip is expanded eagerly: every inner zip is unpacked to a single
    XML under a private scratch dir, then each XML is handed to the parse pool.
    The scratch tree is always removed in the `finally`; the downloaded zip is
    removed too when `delete_source` is set (online deliveries are transient;
    offline source zips are left untouched).
    """
    stem = Path(outer.file_name).stem
    done_marker = out_dir / ".outer" / f"{stem}.done"
    if done_marker.exists():
        return _OuterResult(stem, skipped=True)

    # 1. Acquire the outer zip.
    if client is not None:
        assert delivery_id is not None and outer.file_id is not None
        try:
            with _DOWNLOAD_LOCK:
                client.download_file(product_id, delivery_id, outer.file_id, outer.local_path)
        except BddsError as exc:
            return _OuterResult(stem, errors=[f"download {outer.file_name}: {exc}"])

    work = Path(tempfile.mkdtemp(prefix=f"{stem}_", dir=work_root))
    errors: list[str] = []
    try:
        # 2. Expand the outer zip → Root/DOC/*.zip (inner zips).
        try:
            with zipfile.ZipFile(outer.local_path) as zf:
                zf.extractall(work)
        except (zipfile.BadZipFile, OSError) as exc:
            return _OuterResult(stem, errors=[f"expand {outer.file_name}: {exc}"])

        # 3. Expand every inner zip → one XML, deleting each inner zip as we go.
        xml_dir = work / "_xml"
        xml_dir.mkdir()
        xml_paths: list[Path] = []
        for inner_zip in sorted(work.rglob("*.zip")):
            try:
                with zipfile.ZipFile(inner_zip) as zf:
                    for member in zf.namelist():
                        if not member.lower().endswith(".xml"):
                            continue
                        target = xml_dir / Path(member).name
                        with zf.open(member) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst, length=_CHUNK)
                        xml_paths.append(target)
            except (zipfile.BadZipFile, OSError) as exc:
                errors.append(f"{inner_zip.name}: {exc}")
            finally:
                _unlink_quietly(inner_zip)

        # 4. Parse each not-yet-done inner XML into its TSV part.
        jobs: list[tuple[str, str]] = []
        for xml_path in xml_paths:
            part = out_dir / f"part_{xml_path.stem}.tsv"
            if part.exists():
                continue
            jobs.append((str(xml_path), str(part)))
        for future in [parse_pool.submit(_parse_xml_to_part, job) for job in jobs]:
            err = future.result()
            if err:
                errors.append(err)

        # 5. Only mark the outer done if every inner part succeeded.
        if not errors:
            done_marker.touch()
        return _OuterResult(stem, n_xml=len(xml_paths), n_parsed=len(jobs), errors=errors)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if delete_source:
            _unlink_quietly(outer.local_path)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("could not remove %s: %s", path, exc)


def _collect_local_zips(inputs: list[Path]) -> list[Path]:
    """Resolve offline inputs to a flat, sorted list of outer `.zip` files.

    Directories are scanned non-recursively: the downloaded outer zips sit
    directly inside `BACKFILE_DIR`, and a non-recursive glob avoids descending
    into any stray expanded `Root/DOC/*.zip` trees.
    """
    zips: list[Path] = []
    for path in inputs:
        if path.is_dir():
            zips.extend(sorted(path.glob("*.zip")))
        elif path.is_file() and path.suffix.lower() == ".zip":
            zips.append(path)
        else:
            logger.warning("skipping non-zip input: %s", path)
    return zips


def _build_online_outers(client: BddsClient, product_id: int, staging: Path) -> tuple[int, list[_Outer]]:
    delivery = client.latest_delivery(product_id)
    delivery_id = delivery["deliveryId"]
    files = delivery.get("files") or []
    logger.info(f"latest delivery {delivery_id} ({delivery.get('deliveryName', '')}): {len(files)} file(s)")
    staging.mkdir(parents=True, exist_ok=True)
    outers = [
        _Outer(file_name=f["fileName"], local_path=staging / f["fileName"], file_id=f["fileId"])
        for f in files
        if str(f.get("fileName", "")).lower().endswith(".zip")
    ]
    return delivery_id, outers


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Offline mode: local outer .zip files or directories of them.",
    )
    ap.add_argument(
        "--download",
        action="store_true",
        help="Online mode: enumerate and download the latest backfile delivery.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for part_<name>.tsv output files (created if absent).",
    )
    ap.add_argument(
        "--staging",
        type=Path,
        default=None,
        help="Online mode: where outer zips are downloaded (default: <out-dir>/../backfile_download).",
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Scratch directory for per-outer expansion trees (default: <out-dir>/../backfile_work).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="Parse worker processes (default: min(8, nproc)). Use 1-2 for SATA.",
    )
    ap.add_argument(
        "--in-flight",
        type=int,
        default=8,
        help="Outer zips materialized on disk at once (default: 2). Bounds peak disk use.",
    )
    ap.add_argument(
        "--product-id",
        type=int,
        default=BACKFILE_PRODUCT_ID,
        help=f"BDDS product id for the backfile (default: {BACKFILE_PRODUCT_ID}).",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.download == bool(args.inputs):
        ap.error("provide either --download (online) or one or more local inputs (offline), not both")
    if args.workers < 1 or args.in_flight < 1:
        ap.error("--workers and --in-flight must be >= 1")

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".outer").mkdir(exist_ok=True)
    work_root: Path = args.work_dir or (out_dir.parent / "backfile_work")
    work_root.mkdir(parents=True, exist_ok=True)

    client: BddsClient | None = None
    delivery_id: int | None = None
    if args.download:
        username, password = credentials_from_env()
        client = BddsClient(username, password)
        staging = args.staging or (out_dir.parent / "backfile_download")
        delivery_id, outers = _build_online_outers(client, args.product_id, staging)
    else:
        outers = [_Outer(file_name=p.name, local_path=p) for p in _collect_local_zips(args.inputs)]

    if not outers:
        logger.error("no backfile zips to process")
        return 1

    logger.info(
        "ingesting %d outer zip(s): %d parse worker(s), %d in flight → %s",
        len(outers),
        args.workers,
        args.in_flight,
        out_dir,
    )

    handle = functools.partial(
        _handle_outer,
        client=client,
        product_id=args.product_id,
        delivery_id=delivery_id,
        out_dir=out_dir,
        work_root=work_root,
        delete_source=client is not None,
    )

    errors = 0
    processed = 0
    skipped = 0
    with ProcessPoolExecutor(max_workers=args.workers) as parse_pool:
        bound = functools.partial(handle, parse_pool=parse_pool)
        with ThreadPoolExecutor(max_workers=args.in_flight) as coordinators:
            for result in tqdm(
                coordinators.map(bound, outers),
                total=len(outers),
                unit="zip",
                desc="backfile",
            ):
                if result.skipped:
                    skipped += 1
                    continue
                processed += 1
                for err in result.errors:
                    logger.warning("warning: %s", err)
                    errors += 1

    logger.info(
        "done: %d processed, %d skipped, %d error(s)",
        processed,
        skipped,
        errors,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
