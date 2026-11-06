# =============================================================================
# Forge CLI — Development Task Runner
# =============================================================================
# This Makefile is the single source of truth for every development task.
# It is used by:
#   - Developers, locally (via `make <target>`)
#   - Pre-commit hooks (via `entry: make <target>`)
#   - GitHub Actions CI (via `run: make <target>`)
#
# Design principles:
#   1. One verb per target, composed into higher-level targets.
#   2. Every target is idempotent — safe to run repeatedly.
#   3. `make` with no arguments prints the help.
#   4. No hidden state — every target is a shell command you could type
#      yourself. The Makefile is a shortcut, not a black box.
#
# Usage:
#   make            # print help
#   make check      # run the full quality gate (lint + format + type + test + security)
#   make fix        # auto-fix lint and format issues
# =============================================================================

# Use bash with strict mode for every recipe.
# This catches typos and mid-script failures that `sh` would silently ignore.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

# Tell Make that these targets are not files.
.PHONY: help install sync update lock check lint format type test test-unit \
        test-integration test-contract test-e2e test-security coverage \
        security audit build clean docs pre-commit

# -----------------------------------------------------------------------------
# Default target: print help.
# -----------------------------------------------------------------------------
.DEFAULT_GOAL := help

# -----------------------------------------------------------------------------
# help — self-documenting list of targets.
# -----------------------------------------------------------------------------
# Scans this Makefile for lines starting with `## ` after a target name and
# prints them as a table. This is the standard "self-documenting Makefile"
# pattern: every target's purpose lives in a comment next to its definition.
help: ## Show this help message
	@echo ""
	@echo "Forge CLI — Development Commands"
	@echo "================================"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""

# -----------------------------------------------------------------------------
# Environment setup
# -----------------------------------------------------------------------------
install: ## Install runtime + dev dependencies (first-time setup)
	uv sync --all-extras
	uv run pre-commit install
	@echo "✅ Environment ready. Run 'make check' to verify."

sync: ## Sync dependencies from the lockfile (frozen, reproducible)
	uv sync --frozen --all-extras

update: ## Update all dependencies to the latest allowed versions
	uv lock --upgrade
	uv sync --all-extras
	@echo "✅ Dependencies updated. Review uv.lock before committing."

lock: ## Regenerate uv.lock from pyproject.toml
	uv lock

# -----------------------------------------------------------------------------
# Quality gate — the one command you should always run before pushing.
# -----------------------------------------------------------------------------
check: lint format-check type test security ## Run the full quality gate (CI mirror)
	@echo ""
	@echo "✅ All quality gates passed."

# -----------------------------------------------------------------------------
# Individual quality gates
# -----------------------------------------------------------------------------
lint: ## Lint with Ruff (fails if fixes are needed)
	uv run ruff check --exit-non-zero-on-fix .

format: ## Format with Ruff (writes changes)
	uv run ruff format .

format-check: ## Verify formatting without writing changes
	uv run ruff format --check .

type: ## Type-check with mypy (strict)
	uv run mypy src

# -----------------------------------------------------------------------------
# Testing
# -----------------------------------------------------------------------------
test: ## Run the full test suite with coverage
	uv run pytest

test-unit: ## Run only unit tests
	uv run pytest tests/unit -m unit

test-integration: ## Run only integration tests
	uv run pytest tests/integration -m integration

test-contract: ## Run only contract tests
	uv run pytest tests/contract -m contract

test-e2e: ## Run only end-to-end tests
	uv run pytest tests/e2e -m e2e

test-security: ## Run only security tests
	uv run pytest tests/security -m security

coverage: ## Run tests and open the HTML coverage report
	uv run pytest --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# -----------------------------------------------------------------------------
# Security
# -----------------------------------------------------------------------------
security: ## Run Bandit and pip-audit
	@echo "==> Bandit (static analysis)..."
	uv run bandit -r src/ -ll -q
	@echo "==> pip-audit (dependency vulnerabilities)..."
	@uv export --frozen --no-emit-project --format requirements-txt > /tmp/audit-reqs.txt
	uv run pip-audit --strict -r /tmp/audit-reqs.txt

audit: security ## Alias for `security`

# -----------------------------------------------------------------------------
# Auto-fix helpers
# -----------------------------------------------------------------------------
fix: ## Auto-fix lint and format issues (writes changes)
	uv run ruff check --fix .
	uv run ruff format .
	@echo "✅ Auto-fixes applied. Review with 'git diff' before committing."

# -----------------------------------------------------------------------------
# Build and packaging
# -----------------------------------------------------------------------------
build: ## Build sdist and wheel into dist/
	uv build
	@echo "Artifacts in dist/:"
	@ls -lh dist/

# -----------------------------------------------------------------------------
# Documentation
# -----------------------------------------------------------------------------
docs: ## Serve the documentation locally (requires mkdocs)
	@command -v mkdocs >/dev/null 2>&1 || { \
		echo "mkdocs is not installed. Run: uv add --dev mkdocs-material"; \
		exit 1; \
	}
	uv run mkdocs serve

# -----------------------------------------------------------------------------
# Architecture Decision Records
# -----------------------------------------------------------------------------
# These targets wrap the scripts in scripts/adr/. They are the only supported
# way to create and maintain ADRs; the scripts are considered implementation
# details and may change.
#
# Design notes:
#   - `index.py` exits 1 when it *changes* the index. That is the signal
#     pre-commit and CI use to detect drift. In `adr` and `adr-index`, that
#     exit code is *expected* (the index necessarily changed), so it is
#     tolerated via `|| true`. In `adr-check`, it is *not* tolerated — that
#     is the gate.
#   - `new.sh` already tolerates the index-change exit code internally. The
#     `|| true` here is a belt-and-braces measure in case `new.sh` re-exits
#     non-zero for any reason (a typo in the template, a missing script, a
#     future regression). The ADR is created either way; the next-steps
#     message is the source of truth for "did it work."
# -----------------------------------------------------------------------------

adr: ## Create a new ADR (usage: make adr title="Your decision")
	@if [ -z "$(title)" ]; then \
		echo "Error: 'title' is required." >&2; \
		echo "Usage: make adr title=\"Your decision title\"" >&2; \
		exit 2; \
	fi
	@bash scripts/adr/new.sh "$(title)" || true
	@echo ""
	@echo "✅ ADR workflow complete."
	@echo "   Review the new ADR and commit with:"
	@echo "     git add docs/adr/ && git commit -s"

adr-index: ## Rebuild the ADR index (writes docs/adr/README.md)
	@python3 scripts/adr/index.py || true
	@echo ""
	@echo "✅ ADR index rebuilt."

adr-check: ## Verify the ADR index is up to date (CI gate — fails on drift)
	@if ! python3 scripts/adr/index.py --check; then \
		echo "" >&2; \
		echo "❌ ADR index is stale." >&2; \
		echo "   Run 'make adr-index' to regenerate it, then re-stage and commit." >&2; \
		exit 1; \
	fi
	@echo "✅ ADR index is up to date."

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ .pytest_cache/ .mypy_cache/ .ruff_cache/ \
	       .coverage coverage.xml htmlcov/ .hypothesis/
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	@echo "✅ Cleaned."
