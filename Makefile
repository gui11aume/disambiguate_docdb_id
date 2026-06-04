# ── Configuration (override on the command line) ─────────────────────────────
# Directory of DOCDB back-file XML (full snapshot); populated by `make download-backfile`.
BACKFILE_DIR  ?= backfile
# Directory of DOCDB front-file XML (weekly increments); populated by `make download-frontfile`.
FRONTFILE_DIR ?= frontfile
# Scratch directory for intermediate TSV files.
STAGE         ?= stage
# Final LMDB environment (docs + alias sub-DBs).
LMDB_OUT      ?= out/docdb.lmdb

# Worker processes for XML parsing. For a single cold SATA disk, JOBS=1 or 2 is
# often fastest; raise for SSD/NVMe or when input and output live on separate disks.
JOBS          ?= $(shell nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

UV            ?= uv
PYTHON        ?= $(UV) run python

# External merge sort used to globally order the extracted TSV. Temp files land
# in STAGE so a multi-GB sort does not fill /tmp.
SORT          ?= LC_ALL=C sort
SORTOPTS      ?= --parallel=$(JOBS) -S 20% -T $(STAGE)

SHELL         := /bin/bash

# ── Phony targets ─────────────────────────────────────────────────────────────
.PHONY: all install download-backfile download-frontfile extract sorted core \
        alias update query clean distclean

all: $(LMDB_OUT)/.alias.done

# ── Stage -1: sync the Python environment ────────────────────────────────────
# `uv sync --frozen` installs it verbatim.
install:
	$(UV) sync --frozen

# ── Stage 0: download deliveries from the EPO BDDS endpoint ──────────────────
# Requires EPO_BDDS_USERNAME / EPO_BDDS_PASSWORD in the environment. Back-file
# fetches the latest snapshot; front-file fetches missing weekly deliveries.
download-backfile: | install
	$(PYTHON) download_backfile.py $(BACKFILE_DIR)

download-frontfile: | install
	$(PYTHON) download_frontfile.py $(FRONTFILE_DIR)

# ── Stage 1: parse XML → one TSV part per back-file XML ──────────────────────
# backfile_to_tsv.py writes part_NNNNNN.tsv (one per input file) into STAGE/parts.
# The parts are unsorted; the global ordering happens in stage 2.
extract: $(STAGE)/parts/.done

$(STAGE)/parts/.done: | install
	rm -rf $(STAGE)/parts
	mkdir -p $(STAGE)/parts
	$(PYTHON) backfile_to_tsv.py --workers $(JOBS) --out-dir $(STAGE)/parts $(BACKFILE_DIR)
	touch $@
	@echo "$(STAGE)/parts: $$(find $(STAGE)/parts -name '*.tsv' | wc -l) files"

# ── Stage 2: concatenate + merge-sort the parts into one sorted TSV ──────────
# initialize_core_from_tsv.py loads with append=True, so the input must be
# ascending on column 1 under LC_ALL=C; sort on the first tab-separated field.
sorted: $(STAGE)/sorted.tsv

$(STAGE)/sorted.tsv: $(STAGE)/parts/.done
	find $(STAGE)/parts -name '*.tsv' -print0 \
	    | xargs -0 cat \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 > $(STAGE)/sorted.tsv
	@echo "$(STAGE)/sorted.tsv: $$(wc -l < $(STAGE)/sorted.tsv) rows"

# ── Stage 3: load the docs sub-DB ────────────────────────────────────────────
# The loader wipes and recreates LMDB_OUT, so the sentinel is written afterwards.
core: $(LMDB_OUT)/.core.done

$(LMDB_OUT)/.core.done: $(STAGE)/sorted.tsv
	mkdir -p $(dir $(LMDB_OUT))
	$(PYTHON) initialize_core_from_tsv.py $(LMDB_OUT) $(STAGE)/sorted.tsv
	touch $@

# ── Stage 4: project sorted TSV → 2-column alias input ───────────────────────
# Re-normalises orig_doc_number (col 3) and emits CC/year/era synonyms, drops
# rows where the alias collapses onto the primary key, then sort -u dedups
# byte-identical rows. Genuine alias collisions (same processed alias mapping to
# different primary keys) survive the dedup and are loud-failed by the loader.
$(STAGE)/alias_sorted.tsv: $(STAGE)/sorted.tsv
	$(PYTHON) extract_alias_tsv.py $(STAGE)/sorted.tsv \
	    | $(SORT) $(SORTOPTS) -u > $(STAGE)/alias_sorted.tsv
	@echo "$(STAGE)/alias_sorted.tsv: $$(wc -l < $(STAGE)/alias_sorted.tsv) rows"

# ── Stage 5: load the alias sub-DB into the existing LMDB env ─────────────────
# The loader is idempotent (it drops the alias sub-DB before re-loading). The
# sentinel lets make skip the stage when inputs are unchanged.
alias: $(LMDB_OUT)/.alias.done

$(LMDB_OUT)/.alias.done: $(STAGE)/alias_sorted.tsv $(LMDB_OUT)/.core.done
	$(PYTHON) initialize_alias_from_tsv.py $(LMDB_OUT) $(STAGE)/alias_sorted.tsv
	touch $@

# ── Front-file updates: apply weekly increments in place ─────────────────────
# Refuses to run unless the env is in the `complete` state; applies create/
# amend/delete records straight into the docs sub-DB.
update: $(LMDB_OUT)/.alias.done | install
	$(PYTHON) front_file_to_tsv.py $(LMDB_OUT) $(FRONTFILE_DIR)

# ── Ad-hoc lookups ────────────────────────────────────────────────────────────
# Pipe TSV (free-text <TAB> "<candidate-id>") on stdin, or pass INPUT=<file>.
query: $(LMDB_OUT)/.alias.done | install
	$(PYTHON) query_lmdb.py $(LMDB_OUT) $(INPUT)

# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	rm -rf $(STAGE)

distclean: clean
	rm -rf $(LMDB_OUT)
