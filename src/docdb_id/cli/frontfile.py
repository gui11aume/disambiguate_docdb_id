"""Streaming DOCDB frontfile ingest: download -> expand -> parse -> retain.

The EPO DOCDB frontfile (BDDS product 3) ships weekly incremental deliveries,
each split into one or more outer "part" zips (e.g.
`docdb_xml_202623_Amend_002.zip`) that expand to `Root/DOC/*.zip` -> one XML
each - the same nested layout as the backfile. This command drives the shared
`docdb_id.bdds.ingest` engine with frontfile-specific settings: all-deliveries
enumeration (oldest first) and retention of downloaded archives in staging.

Output is one `part_<outer_stem>.tsv` changelog part per delivery part (all of
its inner XMLs concatenated through a single parser so the `seq` token stays
continuous), ready for the merge-sort + `docdb_id.cli.apply_frontfile` stages.
Resume keys off these TSVs: a part is re-fetched only if its TSV is absent.

Usage:
    # online: fetch missing deliveries, expand, parse
    python -m docdb_id.cli.frontfile --download --out-dir DIR [--staging DIR]
        [--work-dir DIR] [--workers N] [--in-flight N]

    # offline: parse local delivery zips or directories of them
    python -m docdb_id.cli.frontfile <dir-or-zip>... --out-dir DIR [--work-dir DIR]
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

FRONTFILE_PRODUCT_ID = 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="docdb_id.cli.frontfile",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Offline mode: local delivery .zip files or directories of them.",
    )
    ap.add_argument(
        "--download",
        action="store_true",
        help="Online mode: enumerate and download all missing frontfile deliveries.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for part_<outer_stem>.tsv output files (created if absent).",
    )
    ap.add_argument(
        "--staging",
        type=Path,
        default=None,
        help="Online mode: where delivery zips are stored (default: <out-dir>/../frontfile_download).",
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Scratch directory for per-delivery expansion trees (default: <out-dir>/../frontfile_work).",
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
        help="Deliveries expanded on disk at once (default: 2). Bounds peak disk use.",
    )
    ap.add_argument(
        "--product-id",
        type=int,
        default=FRONTFILE_PRODUCT_ID,
        help=f"BDDS product id for the frontfile (default: {FRONTFILE_PRODUCT_ID}).",
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
    work_root: Path = args.work_dir or (out_dir.parent / "frontfile_work")
    staging = args.staging or (out_dir.parent / "frontfile_download")

    config = IngestConfig(
        product_id=args.product_id,
        delivery_mode="all",
        parser_kind="frontfile",
        delete_after_parse=True,
        done_subdir=".delivery",
        out_dir=out_dir,
        work_root=work_root,
        workers=args.workers,
        in_flight=args.in_flight,
        staging=staging if args.download else None,
        offline_inputs=None if args.download else args.inputs,
        skip_download_if_complete=True,
    )
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
