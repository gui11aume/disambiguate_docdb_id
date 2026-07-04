#!/usr/bin/env python3
"""Download the latest EPO DOCDB back-file delivery (BDDS product 14).

The back-file is the full bibliographic snapshot of all DOCDB documents,
delivered as multi-GB zipped XML files. This script always downloads the
*latest* delivery from scratch — the EPO download endpoint ignores HTTP
`Range` requests, so partial-file resume is not possible.

Credentials must be supplied via environment variables:

    export EPO_BDDS_USERNAME=...
    export EPO_BDDS_PASSWORD=...

Usage:
    download_backfile.py [<out-dir>]      # default: ./backfile
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bdds_client import download_latest_delivery

BACKFILE_PRODUCT_ID = 14
DEFAULT_OUT_DIR = Path("backfile")


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
    return download_latest_delivery(BACKFILE_PRODUCT_ID, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
