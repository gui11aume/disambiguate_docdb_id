# ── Configuration (override on the command line) ─────────────────────────────
# Directory of DOCDB backfile XML (full snapshot); populated by `make download-backfile`.
BACKFILE_DIR  ?= backfile
# Directory of DOCDB frontfile XML (weekly increments); populated by `make download-frontfile`.
FRONTFILE_DIR ?= frontfile
# Scratch directory for intermediate TSV files.
STAGE         ?= stage
# Final LMDB environment (docs + alias sub-DBs).
LMDB_OUT      ?= out/docdb.lmdb

# Worker processes for XML parsing. For a single cold SATA disk, JOBS=1 or 2 is
# often fastest; raise for SSD/NVMe or when input and output live on separate disks.
JOBS          ?= $(shell n=$$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4); echo $$(( n < 8 ? n : 8 )))

# Outer backfile zips materialized on disk at once during `make ingest`. Each
# adds roughly one delivery file plus its expanded XML to peak disk use, so the
# backfile never fully lands on disk. Keep small to bound the footprint; raise
# to overlap more downloads with parsing when you have disk to spare.
INFLIGHT      ?= 2

UV            ?= uv
PYTHON        ?= $(UV) run python

# External merge sort used to globally order the extracted TSV. Temp files land
# in STAGE so a multi-GB sort does not fill /tmp.
SORT          ?= LC_ALL=C sort
SORTOPTS      ?= --parallel=$(JOBS) -S 20% -T $(STAGE)

SHELL         := /bin/bash
# Fail a recipe if any command in a pipe fails, not just the last one. Several
# stages pipe `cat | sort | loader`; without this a mid-pipe sort failure would
# be masked by a successful loader and silently build a partial database.
.SHELLFLAGS   := -o pipefail -c

# ── Phony targets ─────────────────────────────────────────────────────────────
.PHONY: all install ingest download-frontfile core \
        alias update query clean distclean

default: backfile-alias


# ── Stage -1: sync the Python environment ────────────────────────────────────
# `uv sync --frozen` installs it verbatim.
install:
	$(UV) sync --frozen

# ── Stage 0+1: stream the backfile (download → expand → parse → cleanup) ────
# ingest_backfile.py processes the backfile as a bounded queue: it downloads
# each outer delivery .zip, expands it (Root/DOC/*.zip → *.xml), parses every
# inner XML into part_<name>.tsv, then deletes the .zip and the expanded tree
# before moving on — so the multi-TB snapshot never fully lands on disk. At most
# INFLIGHT outer zips are materialized at once. Requires EPO_BDDS_USERNAME /
# EPO_BDDS_PASSWORD in the environment. Parts are unsorted; stage 2 orders them.
ingest-backfile: $(STAGE)/backfile_parts/.done

$(STAGE)/backfile_parts/.done: | install
	mkdir -p $(STAGE)/backfile_parts
	$(PYTHON) ingest_backfile.py --download \
	    --out-dir $(STAGE)/backfile_parts \
	    --staging $(STAGE)/backfile_download \
	    --work-dir $(STAGE)/backfile_work \
	    --workers $(JOBS) --in-flight $(INFLIGHT)
	touch $@
	@echo "$(STAGE)/backfile_parts: $$(find $(STAGE)/backfile_parts -name '*.tsv' | wc -l) parts"


# ── Stage 2: concatenate + merge-sort the parts on the fly → load docs sub-DB ─
# initialize_core_from_tsv.py loads with append=True, so its stdin must be
# ascending on column 1 under LC_ALL=C; we cat the parts and sort on the first
# tab-separated field straight into the loader, so no sorted TSV is persisted.
# The loader wipes and recreates LMDB_OUT, so the sentinel is written afterwards.
backfile-core: $(LMDB_OUT)/.core.done

$(LMDB_OUT)/.core.done: $(STAGE)/backfile_parts/.done
	mkdir -p $(dir $(LMDB_OUT))
	find $(STAGE)/backfile_parts -name '*.tsv' -print0 \
	    | xargs -0 cat \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 \
	    | $(PYTHON) initialize_core_from_tsv.py $(LMDB_OUT)
	touch $@


# ── Stage 3: project parts → 2-column alias input ────────────────────────────
# Re-normalises orig_doc_number (col 3) and emits CC/year/era synonyms, drops
# rows where the alias collapses onto the primary key. extract_alias_tsv.py
# processes rows independently, so it reads the unsorted parts directly and
# tags each alias with its source publication date (date_publ, col 5) as a
# third column. We sort on (alias, date_publ) ascending so every alias's rows
# are contiguous with the oldest publication first, then awk keeps the first
# row of each alias — collapsing byte-identical duplicates AND genuine
# collisions (one alias mapping to several primary keys) down to the key with
# the oldest publication date. Because the input is alias-sorted, awk only
# tracks the current alias, so this stays O(1) in memory. The trailing date is
# stripped (cut -f1,2) before the loader, which expects 2 columns.
$(STAGE)/alias_sorted.tsv: $(STAGE)/backfile_parts/.done
	find $(STAGE)/backfile_parts -name '*.tsv' -print0 \
	    | xargs -0 cat \
	    | $(PYTHON) extract_alias_tsv.py \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 -k3,3 \
	    | awk -F'\t' '$$1 != p { print; p = $$1 }' \
	    | cut -f1,2 > $(STAGE)/alias_sorted.tsv
	@echo "$(STAGE)/alias_sorted.tsv: $$(wc -l < $(STAGE)/alias_sorted.tsv) rows"


# ── Stage 5: load the alias sub-DB into the existing LMDB env ─────────────────
# The loader is idempotent (it drops the alias sub-DB before re-loading). The
# sentinel lets make skip the stage when inputs are unchanged.
backfile-alias: $(LMDB_OUT)/.alias.done

$(LMDB_OUT)/.alias.done: $(STAGE)/alias_sorted.tsv $(LMDB_OUT)/.core.done
	$(PYTHON) initialize_alias_from_tsv.py $(LMDB_OUT) $(STAGE)/alias_sorted.tsv
	touch $@




# ── Stage 0 (frontfile): download weekly increments from the BDDS endpoint ──
download-frontfile: | install
	$(PYTHON) download_frontfile.py $(FRONTFILE_DIR)

# ── frontfile updates: apply weekly increments in place ─────────────────────
# Three stages, mirroring the backfile path so parsing never touches LMDB:
#   1. frontfile_to_tsv.py parses the frontfiles into a changelog (one tagged
#      part TSV per input XML; columns key/seq/op/docdb_id/…).
#   2. sort on (key, seq) groups each key's operations and orders them
#      chronologically (the `seq` token encodes delivery order).
#   3. apply_frontfile_to_lmdb.py replays the sorted changelog into the docs
#      sub-DB — the only stage that mutates LMDB. It refuses to run unless the
#      env is in the `complete` state.
update: $(LMDB_OUT)/.alias.done | install
	rm -rf $(STAGE)/frontfile_parts
	mkdir -p $(STAGE)/frontfile_parts
	$(PYTHON) frontfile_to_tsv.py --workers $(JOBS) --out-dir $(STAGE)/frontfile_parts $(FRONTFILE_DIR)
	find $(STAGE)/frontfile_parts -name '*.tsv' -print0 \
	    | xargs -0 cat \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 -k2,2 > $(STAGE)/frontfile_sorted.tsv
	@echo "$(STAGE)/frontfile_sorted.tsv: $$(wc -l < $(STAGE)/frontfile_sorted.tsv) operations"
	$(PYTHON) apply_frontfile_to_lmdb.py $(LMDB_OUT) $(STAGE)/frontfile_sorted.tsv

# ── Ad-hoc lookups ────────────────────────────────────────────────────────────
# Pipe TSV (free-text <TAB> "<candidate-id>") on stdin, or pass INPUT=<file>.
query: $(LMDB_OUT)/.alias.done | install
	$(PYTHON) query_lmdb.py $(LMDB_OUT) $(INPUT)

# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	rm -rf $(STAGE)

distclean: clean
	rm -rf $(LMDB_OUT)
