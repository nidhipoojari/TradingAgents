# TradeDesk AI (Phase 3) - short commands to install, test and run the project.
# Windows has no "make": use build.ps1 with the same target names (e.g. .\build.ps1 test).

PYTHON ?= python3
VENV   ?= .venv
PY     := $(VENV)/bin/python
# Last Phase 2 commit; the code reports cover only what changed after it.
PHASE2_REF ?= 9696262
REPORT_DIR := eval/phase3

.PHONY: help venv install test lint cli dashboard mcp analyze docker doctor clean

help:
	@echo "make install    create .venv and install the project with the dashboard, MCP and Langfuse extras"
	@echo "make test       run the offline test suite (no API keys or network needed)"
	@echo "make lint       run Ruff"
	@echo "make doctor     check Python, packages and .env settings (never prints key values)"
	@echo "make cli        run an analysis in the terminal"
	@echo "make dashboard  open the F1 dashboard at http://127.0.0.1:8501"
	@echo "make mcp        start the F2 MCP server (stdio, for Claude Desktop / Claude Code)"
	@echo "make analyze    write the cloc + lizard reports for Phase 3 code to $(REPORT_DIR)/"
	@echo "make docker     run the CLI in Docker"
	@echo "make clean      delete caches and build files"

$(PY):
	$(PYTHON) -m venv $(VENV)

venv: $(PY)

install: $(PY)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dashboard,mcp,e3]" pytest ruff lizard
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example: add your GOOGLE_API_KEY (and Alpaca paper keys for trading).")

test: $(PY)
	$(PY) -m pytest -q

lint: $(PY)
	$(PY) -m ruff check tradingagents cli

doctor: $(PY)
	$(PY) -m tradingagents.doctor

cli: $(PY)
	$(VENV)/bin/tradingagents

dashboard: $(PY)
	$(VENV)/bin/tradingagents-dashboard

mcp: $(PY)
	$(VENV)/bin/tradingagents-mcp

analyze: $(PY)
	@mkdir -p $(REPORT_DIR)
	git diff --name-only --diff-filter=AM $(PHASE2_REF) HEAD -- 'tradingagents/*.py' 'tradingagents/**/*.py' 'cli/*.py' > $(REPORT_DIR)/new_files.txt
	cloc --by-file --list-file=$(REPORT_DIR)/new_files.txt > $(REPORT_DIR)/cloc.txt
	$(VENV)/bin/lizard $$(cat $(REPORT_DIR)/new_files.txt) > $(REPORT_DIR)/lizard.txt || true
	$(VENV)/bin/lizard -C 10 -w $$(cat $(REPORT_DIR)/new_files.txt) > $(REPORT_DIR)/lizard_warnings.txt || true
	@echo "Reports written to $(REPORT_DIR)/ ($$(wc -l < $(REPORT_DIR)/lizard_warnings.txt | tr -d ' ') functions above CCN 10)"

docker:
	docker compose run --rm tradingagents

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
