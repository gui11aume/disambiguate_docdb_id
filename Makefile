# ── Configuration (override on the command line) ─────────────────────────────
# Scratch directory for downloads, expansion work dirs, and intermediate TSVs.
STAGE        ?= stage
# Final LMDB environment (docs + alias sub-DBs).
LMDB_OUT     ?= out/docdb.lmdb

# Backfile artifacts (full snapshot).
BACKFILE_PARTS   ?= $(STAGE)/backfile_parts
BACKFILE_STAGING ?= $(STAGE)/backfile_download
BACKFILE_WORK    ?= $(STAGE)/backfile_work

# Frontfile artifacts (weekly incremental updates).
FRONTFILE_PARTS   ?= $(STAGE)/frontfile_parts
FRONTFILE_STAGING ?= $(STAGE)/frontfile_download
FRONTFILE_WORK    ?= $(STAGE)/frontfile_work
FRONTFILE_SORTED  ?= $(STAGE)/frontfile_sorted.tsv

# Derived backfile artifacts.
ALIAS_SORTED ?= $(STAGE)/alias_sorted.tsv
CORE_DONE    ?= $(LMDB_OUT)/.core.done
ALIAS_DONE   ?= $(LMDB_OUT)/.alias.done

# Worker processes for XML parsing. For a single cold SATA disk, NJOBS=1 or 2 is
# often fastest; raise for SSD/NVMe or when input and output live on separate disks.
NCPU     := $(shell nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
NJOBS   ?= $(shell echo $$(( $(NCPU) < 8 ? $(NCPU) : 8 )))

# Outer zips materialized on disk at once. Each adds roughly one delivery file
# plus its expanded XML to peak disk use, so the backfile never fully lands on
# disk. Keep small to bound the footprint; raise to overlap more downloads with
# parsing when you have disk to spare.
INFLIGHT ?= 2

# Frontfile deliveries are weekly and small, so there's little to gain from
# overlapping downloads; default to one at a time.
FRONTFILE_INFLIGHT ?= 1

UV       ?= uv
PYTHON   ?= $(UV) run python

# External merge sort used to globally order the extracted TSV. Temp files land
# in STAGE so a multi-GB sort does not fill /tmp.
SORT     ?= LC_ALL=C sort
SORTOPTS ?= --parallel=$(NJOBS) -S 20% -T $(STAGE)

SHELL       := /bin/bash
# Fail a recipe if any command in a pipe fails, not just the last one. Several
# stages pipe `cat | sort | loader`; without this a mid-pipe sort failure would
# be masked by a successful loader and silently build a partial database.
.SHELLFLAGS := -o pipefail -c

# Concatenate all part TSVs under a directory to stdout.
cat_parts = find $(1) -name '*.tsv' -print0 | xargs -0 cat

# ── Phony targets ─────────────────────────────────────────────────────────────
.PHONY: default all install lint test \
        backfile ingest-backfile backfile-core backfile-alias \
        frontfile ingest-frontfile apply-frontfile prune-aliases \
        ingest ingest-backfile ingest-frontfile update query show-meta clean distclean

default: install
all: apply-backfile apply-frontfile


# ── Development ────────────────────────────────────────────────────────────────
# `uv sync --frozen` installs the package and locked dependencies verbatim.
install:
	$(UV) sync --frozen --extra dev

lint:
	$(UV) run ruff check

test:
	$(UV) run pytest tests/ -q


# ── Backfile pipeline (full snapshot) ─────────────────────────────────────────
apply-backfile: backfile-alias

# Download, expand, parse, and clean up the backfile into unsorted part TSVs.
# This target is sentinel-based because a backfile delivery is a fixed snapshot:
# once every outer zip has been successfully parsed, the part set is complete.
ingest-backfile: $(BACKFILE_PARTS)/.done

$(BACKFILE_PARTS)/.done: | install
	mkdir -p $(BACKFILE_PARTS)
	$(PYTHON) -m docdb_id.cli.backfile --download \
	    --out-dir $(BACKFILE_PARTS) \
	    --staging $(BACKFILE_STAGING) \
	    --work-dir $(BACKFILE_WORK) \
	    --workers $(NJOBS) --in-flight $(INFLIGHT)
	touch $@
	@echo "$(BACKFILE_PARTS): $$(find $(BACKFILE_PARTS) -name '*.tsv' | wc -l) parts"

# Sort all backfile parts by canonical key and load the docs sub-DB. The loader
# wipes and recreates LMDB_OUT, so the sentinel is written only afterwards.
backfile-core: $(CORE_DONE)

$(CORE_DONE): $(BACKFILE_PARTS)/.done
	mkdir -p $(dir $(LMDB_OUT))
	$(call cat_parts,$(BACKFILE_PARTS)) \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 \
	    | $(PYTHON) -m docdb_id.cli.core $(LMDB_OUT)
	touch $@

# Project the backfile parts into alias candidates, sort by (alias, date), keep
# the oldest mapping per alias, and strip the date before loading the alias DB.
$(ALIAS_SORTED): $(BACKFILE_PARTS)/.done
	$(call cat_parts,$(BACKFILE_PARTS)) \
	    | $(PYTHON) -m docdb_id.cli.alias_extract \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 -k3,3 \
	    | awk -F'\t' '$$1 != p { print; p = $$1 }' \
	    | cut -f1,2 > $(ALIAS_SORTED)
	@echo "$(ALIAS_SORTED): $$(wc -l < $(ALIAS_SORTED)) rows"

# Load the alias sub-DB into the existing LMDB env. The loader is idempotent: it
# drops and recreates the alias DB before re-loading.
backfile-alias: $(ALIAS_DONE)

$(ALIAS_DONE): $(ALIAS_SORTED) $(CORE_DONE)
	$(PYTHON) -m docdb_id.cli.alias_load $(LMDB_OUT) $(ALIAS_SORTED)
	touch $@


# ── Frontfile pipeline (weekly increments) ───────────────────────────────────
frontfile: apply-frontfile

# Download, expand, parse, and clean up frontfile delivery zips. This target is
# intentionally phony: new BDDS deliveries can appear at any time, and the
# frontfile CLI skips parts whose TSV already exists.
ingest-frontfile: | install
	mkdir -p $(FRONTFILE_PARTS)
	$(PYTHON) -m docdb_id.cli.frontfile --download \
	    --out-dir $(FRONTFILE_PARTS) \
	    --staging $(FRONTFILE_STAGING) \
	    --work-dir $(FRONTFILE_WORK) \
	    --workers $(NJOBS) --in-flight $(FRONTFILE_INFLIGHT) \
	    --lmdb $(LMDB_OUT)
	@echo "$(FRONTFILE_PARTS): $$(find $(FRONTFILE_PARTS) -name '*.tsv' | wc -l) parts"

# Sort the accumulated changelog parts by (key, seq), then apply them to the
# docs sub-DB and record the applied frontfile part stems in the meta sub-DB.
#
# Deliberately does not list $(ALIAS_DONE) as a prerequisite: that would pull
# in the full backfile chain ($(ALIAS_SORTED), $(CORE_DONE),
# $(BACKFILE_PARTS)/.done) whenever any of those staging files are missing,
# even if the alias DB is already loaded — e.g. after `make clean`, or on a
# host that only ever received the finished LMDB. Frontfile updates apply to
# an already-built LMDB and must never trigger a backfile rebuild; if the
# alias DB really isn't loaded yet, fail fast instead of silently re-deriving it.
apply-frontfile: | install
	@test -f $(ALIAS_DONE) || { echo "error: $(ALIAS_DONE) missing — run 'make backfile-alias' first" >&2; exit 1; }
	$(MAKE) ingest-frontfile
	$(call cat_parts,$(FRONTFILE_PARTS)) \
	    | $(SORT) $(SORTOPTS) -t $$'\t' -k1,1 -k2,2 > $(FRONTFILE_SORTED)
	@echo "$(FRONTFILE_SORTED): $$(wc -l < $(FRONTFILE_SORTED)) operations"
	$(PYTHON) -m docdb_id.cli.apply_frontfile $(LMDB_OUT) $(FRONTFILE_SORTED) \
	    $$(find $(FRONTFILE_PARTS) -name '*.tsv' -print)
	$(MAKE) prune-aliases
	# Applied part stems are now durably recorded in the LMDB meta sub-DB
	# (see load_applied_frontfile_parts), so local staging is disposable:
	# wipe it so next run starts clean instead of accumulating forever.
	rm -rf $(FRONTFILE_PARTS) $(FRONTFILE_STAGING) $(FRONTFILE_WORK) $(FRONTFILE_SORTED)

# Remove aliases whose target key no longer exists in the docs sub-DB and record
# (via the alias_no_dangling meta key) that the alias DB is free of dangling
# entries. A frontfile apply can create such orphans, so this runs after it; it
# is also safe to invoke on its own.
prune-aliases: | install
	$(PYTHON) -m docdb_id.cli.alias_gc $(LMDB_OUT)


# ── Ad-hoc lookups ────────────────────────────────────────────────────────────
# Pipe TSV (free-text <TAB> "<candidate-id>") on stdin, or pass INPUT=<file>.
query: $(ALIAS_DONE) | install
	$(PYTHON) -m docdb_id.cli.query $(LMDB_OUT) $(INPUT)

# Dump the LMDB meta sub-DB (build status, timestamps, applied frontfile parts).
show-meta:
	$(PYTHON) -m docdb_id.cli.show_meta $(LMDB_OUT)


# ── Deployment ────────────────────────────────────────────────────────────────
COMPOSE ?= docker compose -f /opt/docdb/docker-compose.yml

.PHONY: up down restart redeploy reload logs ps

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart $(SERVICE)

# Always reload nginx to get the new IP on the Docker bridge network.
redeploy:
	$(COMPOSE) up -d --build $(SERVICE)
	$(MAKE) reload

reload:
	$(COMPOSE) exec nginx nginx -s reload

logs:
	$(COMPOSE) logs -f --tail=50 $(SERVICE)

ps:
	$(COMPOSE) ps


# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	rm -rf $(STAGE)

distclean: clean
	rm -rf $(LMDB_OUT)
