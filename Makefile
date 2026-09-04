.DEFAULT_GOAL := help
PY := .venv/bin/python
SHELL := /bin/bash

.PHONY: help install test test-v run dry-run stats recent eval feedback clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install dependencies
	uv venv
	uv pip install -e '.[dev]'

test: ## Run the test suite (no network)
	$(PY) -m pytest -q

test-v: ## Run the test suite verbosely
	$(PY) -m pytest -v

run: ## Full pipeline: fetch, dedupe, triage, notify
	$(PY) -m jobpipe.cli run

dry-run: ## Full pipeline with no writes and no pushes
	$(PY) -m jobpipe.cli run --dry-run

stats: ## Database and configuration summary
	$(PY) -m jobpipe.cli stats

recent: ## Show the most recent postings
	$(PY) -m jobpipe.cli recent --limit 25

eval: ## Regenerate EVAL.md from the stored run reports
	$(PY) -m jobpipe.eval
	@echo
	@echo "--- exclusion sample (false-negative check) ---"
	@$(PY) -m jobpipe.cli audit-exclusions --sample 20 2>/dev/null || true
	@echo
	@echo "--- baseline suppressions (over-collapse check) ---"
	@$(PY) -m jobpipe.cli audit-suppressions --sample 15 2>/dev/null || true

feedback: ## Print the FEEDBACK.md copy-paste block plus the latest EVAL summary
	@if [ -f FEEDBACK.md ]; then \
		awk '/^```$$/{n++; if(n%2==1){buf=""; next} else {last=buf; next}} \
		     n%2==1{buf = buf $$0 "\n"} END{printf "%s", last}' FEEDBACK.md; \
	else \
		echo "(FEEDBACK.md is a local working note and is not in the repo.)"; \
	fi
	@echo
	@if [ -f EVAL.md ]; then \
		echo "--- latest EVAL.md summary ---"; \
		sed -n '/^## Summary/,/^## /p' EVAL.md | sed '$$d'; \
	else \
		echo "(EVAL.md not generated yet - run \`make eval\` once there is run history.)"; \
	fi

clean: ## Remove caches and build artifacts (never touches data/)
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache *.egg-info src/*.egg-info dist build
