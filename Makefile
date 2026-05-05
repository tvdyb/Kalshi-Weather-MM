.PHONY: install run paper test lint fmt clean backfill schema

PY ?= python3.11
VENV ?= .venv
PIP = $(VENV)/bin/pip
PYRUN = $(VENV)/bin/python

$(VENV)/bin/activate:
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip wheel

install: $(VENV)/bin/activate
	$(PIP) install -e ".[dev]"

run: install
	$(PYRUN) -m kweather.main

paper: install
	$(PYRUN) -m kweather.main --paper

test: install
	$(VENV)/bin/pytest -q

lint: install
	$(VENV)/bin/ruff check kweather tests

fmt: install
	$(VENV)/bin/ruff format kweather tests
	$(VENV)/bin/ruff check --fix kweather tests

backfill: install
	$(PYRUN) scripts/backfill_theos.py

schema: install
	$(PYRUN) -c "import asyncio; from kweather.storage.db import init_db; asyncio.run(init_db())"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache build dist *.egg-info
