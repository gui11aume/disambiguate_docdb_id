#!/usr/bin/env python3
"""Download missing EPO DOCDB front-file deliveries (BDDS product 3).

Front-files are weekly incremental updates (create/delete/amend records)
layered on top of the back-file snapshot. They are small relative to the
back-file — typically a handful of MB per delivery. This script lists all
available deliveries and downloads only files that are missing locally (or
whose local size does not match the API metadata). The EPO download endpoint
ignores HTTP `Range` requests, so partial-file resume is not possible.

Credentials must be supplied via environment variables:

    export EPO_BDDS_USERNAME=...
    export EPO_BDDS_PASSWORD=...

Usage:
    download_frontfile.py [<out-dir>]      # default: ./frontfile
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bdds_client import download_all_deliveries

FRONTFILE_PRODUCT_ID = 3
DEFAULT_OUT_DIR = Path("frontfile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "out_dir",
        type=Path,
        nargs="?",
        default=DEFAULT_OUT_DIR,
        help=f"Output directory, created if absent (default: {DEFAULT_OUT_DIR}).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return download_all_deliveries(FRONTFILE_PRODUCT_ID, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
