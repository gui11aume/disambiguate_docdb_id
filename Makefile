# ── Configuration (override on the command line) ─────────────────────────────
DATA         ?= data          # root directory (or file list) of XML/XML.gz files
LMDB_OUT     ?= out/docdb.lmdb
JOBS         ?= $(shell nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
# Disk read concurrency. Default 1 is tuned for slow SATA/HDD/NFS where
# parallel reads cause seek thrashing. Raise to 4 for SSD, 8-16 for NVMe.
# Decoupled from JOBS, which controls parser/CPU threads.
IO_JOBS      ?= 1
EXTRACT      := docdb-tools/target/release/extract
MERGE        := docdb-tools/target/release/merge
PYTHON       ?= uv run python

# ── Phony targets ─────────────────────────────────────────────────────────────
.PHONY: all build clean distclean stage/parts

all: $(LMDB_OUT)/.layer_1.done

# ── Build Rust binaries ──────────────────────────────────────────────────────
build: $(EXTRACT) $(MERGE)

$(EXTRACT) $(MERGE): docdb-tools/Cargo.toml $(wildcard docdb-tools/src/*.rs docdb-tools/src/bin/*.rs)
	cargo build --release --manifest-path docdb-tools/Cargo.toml \
	    --target-dir docdb-tools/target

# ── Stage 1: parse XML → one sorted TSV per XML file ─────────────────────────
# Two equivalent extractors are available. Select with EXTRACTOR=rust|python.
# Both sort each part in memory before writing (no GNU sort pass needed).
EXTRACTOR    ?= rust

stage/parts:
	rm -rf stage/parts
	mkdir -p stage/parts
ifeq ($(EXTRACTOR),python)
	$(PYTHON) build_lmdb_with_backfile.py --workers $(IO_JOBS) \
	    --out-dir stage/parts $(DATA)
else
	$(MAKE) $(EXTRACT)
	$(EXTRACT) --threads $(JOBS) --io-threads $(IO_JOBS) \
	    --out-dir stage/parts $(DATA)
endif
	@echo "stage/parts: $$(find stage/parts -name '*.tsv' | wc -l) files"

# ── Stage 2: merge sorted TSVs ───────────────────────────────────────────────
# Streaming k-way merge replaces GNU sort. The merge binary accepts the parts
# directory directly and does bounded multi-pass merging if there are many files.
stage/sorted.tsv: stage/parts $(MERGE)
	$(MERGE) stage/parts > stage/sorted.tsv
	@echo "stage/sorted.tsv: $$(wc -l < stage/sorted.tsv) rows"

# ── Stage 3: load LMDB ───────────────────────────────────────────────────────
$(LMDB_OUT): stage/sorted.tsv
	mkdir -p $(dir $(LMDB_OUT))
	$(PYTHON) load_lmdb_from_tsv.py $(LMDB_OUT) stage/sorted.tsv

# ── Stage 4: project sorted TSV → 2-column layer_1 input ─────────────────────
# Re-normalises orig_doc_number (col 3) with helpers.processed_doc_number,
# drops empties and rows where the alias collapses onto the primary key,
# then sort -u dedups byte-identical rows. Genuine alias collisions
# (same processed alias mapping to different primary keys) survive the
# dedup and are loud-failed by the loader.
stage/layer1_sorted.tsv: stage/sorted.tsv
	$(PYTHON) extract_layer1_tsv.py stage/sorted.tsv | LC_ALL=C sort -u > stage/layer1_sorted.tsv
	@echo "stage/layer1_sorted.tsv: $$(wc -l < stage/layer1_sorted.tsv) rows"

# ── Stage 5: load the layer_1 sub-DB into the existing LMDB env ──────────────
# Sentinel file is created on success so make can skip re-running when
# inputs are unchanged. The loader itself is idempotent: it drops the
# layer_1 sub-DB before re-loading.
$(LMDB_OUT)/.layer_1.done: stage/layer1_sorted.tsv $(LMDB_OUT)
	$(PYTHON) initialize_layer1_from_tsv.py $(LMDB_OUT) stage/layer1_sorted.tsv
	touch $(LMDB_OUT)/.layer_1.done

# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	rm -rf stage

distclean: clean
	rm -rf $(LMDB_OUT)
	cargo clean --manifest-path docdb-tools/Cargo.toml
