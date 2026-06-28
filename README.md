# disambiguate_docdb_id

The EPO's DOCDB corpus assigns each patent publication a canonical identifier assembled from XML attributes in the exchange document. In practice, callers rarely have that exact identifier: they may hold the publishing office's native number, a variant with different leading zeros, or a number whose form was later amended. A direct key lookup misses all of these cases.

This project builds a local LMDB index from the official BDDS data and resolves candidate identifiers to the correct DOCDB record, even when the candidate does not match the canonical key.

## Installation

The project requires **Python 3.10** and [uv](https://docs.astral.sh/uv/).

```bash
cd disambiguate_docdb_id
make install
```

`make install` runs `uv sync --frozen`, which creates a virtual environment and installs all locked dependencies.

To ingest data from the EPO Bulk Data Distribution Service you will also need credentials:

```bash
export EPO_BDDS_USERNAME=...
export EPO_BDDS_PASSWORD=...
```

## Quick start

The [Makefile](Makefile) drives the full pipeline. Scratch data lands in `stage/` by default and the finished database is written to `out/docdb.lmdb`.

```bash
# Build the full snapshot (backfile) — run once, takes several hours
make apply-backfile

# Pull in weekly updates (frontfile) — run regularly
make apply-frontfile

# Resolve candidate IDs: pipe TSV (<free text>\t"<CC><number><kind>") on stdin
echo 'example\t"US20130143024A1"' | make query

# Show build status and which frontfile deliveries have been applied
make show-meta
```

Paths and parallelism can be overridden on the command line:

```bash
make apply-backfile LMDB_OUT=/data/docdb.lmdb NJOBS=4
```

---

## Background

### The problem

DOCDB records are keyed by a canonical publication number assembled from the `country`, `doc-number`, and `kind` attributes of the `<exch:exchange-document>` XML element. That doc-number is the DOCDB internal form, which often differs from the number the publishing office uses in its own systems. When a caller supplies an office-native number, a variant with different leading zeros, or a number that was later corrected, a direct key lookup returns nothing.

### The approach

Two complementary lookup tables make disambiguation possible. The primary table (`docs`) stores each document under its canonical key. A secondary table (`alias`) maps alternative forms of the same number back to the canonical key, so lookups that would otherwise miss can be resolved with one extra hop.

The normalisation applied to alias candidates when the database is built is identical to what the lookup code applies to a query at runtime. This means the alias table can be generated entirely offline from the source XML, with no special handling required on the query path.

---

## Database layout

The on-disk store is a single LMDB environment with three named sub-databases:

| Sub-DB | Contents |
|--------|----------|
| `docs` | Canonical key → msgpack list of records `[docdb_id, inventor, date_publ, family_id]` |
| `alias` | Normalised alternate number → canonical key |
| `meta` | Build status, timestamps, applied frontfile parts, alias hygiene markers |

Keys take the form `CC` + doc-number with leading zeros stripped (for example `US2013143024`). Multiple `docdb_id` values can share a single key when DOCDB groups document variants under the same publication number.

---

## Pipeline

```
EPO BDDS                    parse XML                       sort + load
─────────                   ─────────                       ───────────
backfile (product 14)  ──►  part_*.tsv (6 cols)       ──►  docs sub-DB
                       └──► alias candidates           ──►  alias sub-DB

frontfile (product 3)  ──►  part_*.tsv (8 cols)        ──►  apply changelog
  weekly deliveries         key · seq · op · …               (A / C / D)
```

### Backfile

The backfile is a terabyte-scale snapshot split into thousands of outer delivery zips. Each outer zip expands to a `Root/DOC/` tree of inner zips, and each inner zip contains a single DOCDB XML file. The ingest engine downloads and expands outer zips one batch at a time (governed by `INFLIGHT`), parses each inner XML with a streaming lxml SAX target that builds no DOM in memory, and writes one TSV part per inner file before discarding the outer zip. Peak disk use is therefore bounded regardless of total corpus size.

Once all parts are written, they are sorted globally by canonical key using `LC_ALL=C sort` and loaded into the `docs` sub-DB in a single sequential pass. A second pass projects each row to its alias candidate, sorts by `(alias, date_publ)` so that collisions resolve in favour of the oldest publication, and loads the result into `alias`.

### Frontfile

The frontfile ships weekly incremental updates using the same nested zip layout. Each outer delivery part is parsed into a single changelog TSV; a monotonic `seq` token encodes the file index and the per-file position so parts can be merged into chronological order with a plain lexicographic sort.

The apply step reads the sorted changelog and replays each operation:

- **C (create) / A (amend):** upsert the record and refresh any aliases derived from the original doc-number.
- **D (delete):** remove the matching record and drop aliases that now point nowhere.

After applying, `alias_gc` prunes any remaining dangling aliases and marks the alias sub-DB as verified clean.

Both stages support resuming an interrupted run. The backfile tracks completion per outer zip with a sentinel file; the frontfile treats the presence of a TSV part as proof that the corresponding delivery has been fully processed.

### Query resolution

`docdb-query` reads TSV input (`<free text>\t"<CC><number><kind>"`), normalises the candidate by stripping the kind code and leading zeros from the doc-number, and resolves it in two steps:

1. Look up the normalised form directly in `docs`.
2. If that misses, look it up in `alias` to obtain the canonical key, then look up the canonical key in `docs`.

A small number of key-shape fallbacks handle common formatting variants, such as optional zero padding between the country code and the number body. Results are written as a third TSV column containing a JSON array of matching records, or an empty array when nothing is found.

---

## Repository map

```
disambiguate_docdb_id/
├── Makefile                    End-to-end pipeline orchestration
├── pyproject.toml              Package metadata and CLI entry points
├── src/docdb_id/
│   ├── bdds/
│   │   ├── client.py           EPO BDDS OAuth2 client (enumerate and download deliveries)
│   │   └── ingest.py           Shared streaming download → expand → parse engine
│   ├── parse/
│   │   └── docdb_target.py     lxml SAX targets: BackfileTarget and FrontfileTarget
│   ├── alias/
│   │   └── extract.py          Per-country heuristics for office-native → alias mapping
│   ├── normalize.py            XML entity normalization and publication-number canonicalization
│   ├── country_codes.py        Valid two-letter country codes
│   ├── store/
│   │   ├── schema.py           LMDB sub-DB names, meta keys, and shared record types
│   │   ├── core.py             Sorted TSV → docs sub-DB loader
│   │   ├── alias.py            Alias TSV → alias sub-DB loader; add and remove helpers
│   │   ├── apply_frontfile.py  Replay a sorted changelog against docs and alias
│   │   └── query.py            Two-tier lookup: direct hit, then alias follow-up
│   └── cli/                    Thin argument-parser wrappers, also registered as console scripts
│       ├── backfile.py         docdb-backfile
│       ├── frontfile.py        docdb-frontfile
│       ├── core.py             docdb-core
│       ├── alias_extract.py    docdb-alias-extract
│       ├── alias_load.py       docdb-alias-load
│       ├── apply_frontfile.py  docdb-apply-frontfile
│       ├── alias_gc.py         docdb-alias-gc  (garbage-collect dangling aliases)
│       ├── query.py            docdb-query
│       └── show_meta.py        docdb-show-meta
└── tests/                      Unit and integration tests for alias, ingest, and apply
```

For someone new to the codebase, a useful reading order is:

1. `parse/docdb_target.py` — understand what fields are extracted from DOCDB XML and how the canonical key is constructed.
2. `store/schema.py` — the LMDB layout and the meaning of each meta key.
3. `Makefile` — how the stages connect, including the sort commands, the awk collapse step, and sentinel files.
4. `bdds/ingest.py` — the streaming ingest engine, its parallelism model, and resume semantics.
5. `alias/extract.py` — the disambiguation heuristics, including per-country normalisation rules.
6. `store/query.py` — how a candidate identifier is resolved at lookup time.

## Development

```bash
make install              # create the virtual environment and install dependencies
uv run pytest             # run the test suite
uv run ruff check src tests   # lint
```

Individual CLI modules can also be invoked directly, for example:

```bash
uv run python -m docdb_id.cli.query out/docdb.lmdb input.tsv
```

## License

MIT — see [LICENSE](LICENSE).
