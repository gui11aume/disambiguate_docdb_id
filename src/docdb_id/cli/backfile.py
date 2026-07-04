"""Streaming DOCDB backfile ingest: download -> expand -> parse -> cleanup.

The EPO DOCDB backfile (BDDS product 14) is a terabyte-scale snapshot delivered
as thousands of "outer" zip files. Each outer zip expands to a small tree whose
`DOC/*.zip` inner zips each hold a single `DOCDB-...-NNNN.xml`.

Downloading and unpacking the whole product at once would need terabytes of free
disk. This command drives the shared `docdb_id.bdds.ingest` engine with
backfile-specific settings: nested zip expansion, latest-delivery enumeration,
and deletion of downloaded outer zips after parsing.

Usage:
    # online: enumerate + download the latest backfile delivery, then parse
    python -m docdb_id.cli.backfile --download --out-dir DIR [--staging DIR]
        [--work-dir DIR] [--workers N] [--in-flight N]

    # offline: parse a local directory of already-downloaded outer .zip files
    python -m docdb_id.cli.backfile <dir-or-zip>... --out-dir DIR [--work-dir DIR]
        [--workers N] [--in-flight N]

Online mode needs EPO_BDDS_USERNAME / EPO_BDDS_PASSWORD in the environment.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from docdb_id.bdds.ingest import IngestConfig, run

BACKFILE_PRODUCT_ID = 14


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="docdb_id.cli.backfile",
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
        default=2,
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
    work_root: Path = args.work_dir or (out_dir.parent / "backfile_work")
    staging = args.staging or (out_dir.parent / "backfile_download")

    config = IngestConfig(
        product_id=args.product_id,
        delivery_mode="latest",
        parser_kind="backfile",
        delete_after_parse=True,
        done_subdir=".outer",
        out_dir=out_dir,
        work_root=work_root,
        workers=args.workers,
        in_flight=args.in_flight,
        staging=staging if args.download else None,
        offline_inputs=None if args.download else args.inputs,
    )
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
