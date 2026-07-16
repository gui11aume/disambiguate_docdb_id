"""Project the main 6-column backfile TSV down to the alias 3-column input.

Reads the 6-column backfile TSV from stdin (or a file argument) and writes the
3-column alias input (`alias \t key \t date_publ`) to stdout. The output is
intentionally unsorted: the downstream loader requires ascending order on column
1, so this is meant to be sorted on `(alias, date_publ)` and collapsed to one
row per alias before being fed to `docdb_id.cli.alias_load`.

Usage:
    python -m docdb_id.cli.alias_extract [<input.tsv>]      # input defaults to stdin
"""

from __future__ import annotations

import sys
from pathlib import Path

from docdb_id.alias.extract import emit


def main(argv: list[str] | None = None) -> int:
    """Project a 6-column backfile TSV down to a 3-column alias input.

    Args:
        argv: Command-line argument list. Optional first element is an input TSV
            file path (reads stdin if omitted).

    Returns:
        Exit code (0 on success, 2 on usage error).
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("usage: python -m docdb_id.cli.alias_extract [<input.tsv>]", file=sys.stderr)
        return 2

    out = sys.stdout.buffer
    if argv:
        with Path(argv[0]).open("rb") as src:
            counts = emit(src, out)
    else:
        counts = emit(sys.stdin.buffer, out)

    (
        n_in,
        n_out,
        n_zero_stripped,
        n_wo_two_digit_year,
        n_jp_era,
        n_jp_era_padded,
        n_skipped_equal,
        n_skipped_pattern,
    ) = counts

    print(
        f"alias extract: {n_in:,} rows in, {n_out:,} aliases out, "
        f"{n_zero_stripped:,} zero-stripped key aliases, "
        f"{n_wo_two_digit_year:,} WO two-digit-year aliases, "
        f"{n_jp_era:,} JP era aliases, "
        f"{n_jp_era_padded:,} JP era padded aliases, "
        f"{n_skipped_equal:,} skipped (alias == key), "
        f"{n_skipped_pattern:,} skipped (alias pattern)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
