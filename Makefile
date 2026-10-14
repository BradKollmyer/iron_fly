PYTHON ?= .venv/bin/python
ifeq ($(wildcard $(PYTHON)),)
PYTHON := python3
endif

# Editable installs can flake; always put src/ on the path for local runs.
export PYTHONPATH := src$(if $(PYTHONPATH),:$(PYTHONPATH),)

REPLAY_BOOTSTRAP_DAYS ?= 10
ARGS ?=

BAKE_DOCKER_CONTEXT ?= mac-mini
BAKE_IMAGE ?= iron-fly-bake:local

.PHONY: help run-local run-ui run-docker download-replay upload-replay bake bake-image bake-upload \
	backtest baketest backtest-docker test lint format build hydrate

help:
	@echo "run-local          hydrate last $(REPLAY_BOOTSTRAP_DAYS) + serve http://localhost:\$$LISTEN_PORT"
	@echo "run-docker         docker compose up --build"
	@echo "hydrate            download last $(REPLAY_BOOTSTRAP_DAYS) days from R2"
	@echo "download-replay    ARGS='--last 30' (or --from/--to/--all)"
	@echo "upload-replay      gzip local replay/ → R2 (needs R2_* write keys + pip install '.[r2]')"
	@echo "bake-image         build $(BAKE_IMAGE) on Docker context $(BAKE_DOCKER_CONTEXT)"
	@echo "bake               remote bake on mini data network; docker cp JSON home"
	@echo "bake-upload        bake then upload-replay"
	@echo "backtest           ARGS='config/default.yaml --last 10'  (host Python)"
	@echo "backtest-docker    same ARGS, inside Docker (hydrates R2 into the replay volume)"
	@echo "lint / format / test / build"

hydrate:
	$(PYTHON) scripts/download-replay.py --last $(REPLAY_BOOTSTRAP_DAYS)

run-local: hydrate
	DESK_MODE=backtest-public $(PYTHON) -m iron_fly
# Same as run-local but skip hydrate if replay/ already warm.
run-ui:
	DESK_MODE=backtest-public $(PYTHON) -m iron_fly

run-docker:
	docker compose up --build

download-replay:
	$(PYTHON) scripts/download-replay.py $(ARGS)

upload-replay:
	$(PYTHON) scripts/upload-replay-r2.py $(ARGS)

bake-image:
	docker --context $(BAKE_DOCKER_CONTEXT) build -f Dockerfile.bake -t $(BAKE_IMAGE) .

bake:
	$(PYTHON) scripts/bake-replay.py

bake-upload: bake upload-replay

# Local Python and Docker share this CLI. Example:
#   make backtest ARGS='config/default.yaml --last 10'
#   make backtest-docker ARGS='config/1545-20-market.yaml --day 2026-10-12'
# baketest is a silent typo alias for backtest.
backtest baketest:
	$(PYTHON) -m iron_fly.backtest $(ARGS)

backtest-docker:
	docker compose run --rm --build web python -m iron_fly.backtest $(ARGS)

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests scripts

format:
	$(PYTHON) -m ruff format src tests scripts

build:
	docker compose build
