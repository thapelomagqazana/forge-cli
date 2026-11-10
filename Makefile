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
#   make check      # run the full quality gate (lint + format + type + test + security + models + adr)
#   make fix        # auto-fix lint and format issues
# =============================================================================

# Use bash with strict mode for every recipe.
# This catches typos and mid-script failures that `sh` would silently ignore.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

# Tell Make that these targets are not files.
.PHONY: help install sync update lock check lint format format-check type \
        test test-unit test-integration test-contract test-e2e test-security \
        test-file coverage security audit fix build clean docs pre-commit \
        adr adr-index adr-check models models-check

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
# The composition is deliberate and must match the CI workflow's job
# list exactly. If a gate is added to one and not the other, local
# `make check` and CI disagree, which is the source of "works on my
# machine" failures that erode trust in the gate.
#
# The current composition:
#
#   lint          — Ruff, exit-non-zero-on-fix (a fix would be a diff)
#   format-check  — Ruff format, --check (a rewrite would be a diff)
#   type          — mypy --strict
#   test          — pytest, with coverage gate enforced
#   security      — Bandit + pip-audit
#   models-check  — Pydantic models in sync with the JSON Schema
#   adr-check     — ADR index in sync with the ADR files
#
# The two `*-check` targets are checks-without-writes: they fail if the
# working tree *would* change, which is the correct semantics for a CI
# mirror. `make adr-index` and `make models` are the write variants,
# run manually when the user wants to update the artefacts.
check: lint format-check type test security models-check adr-check ## Run the full quality gate (CI mirror)
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
# `test` runs every test pytest collects, which includes the adversarial
# suite (`tests/adversarial/`). That is deliberate: the adversarial tests
# are ordinary pytest tests and they run under `-m` filters as expected.
# They are *also* runnable in isolation via `test-security` (below) for
# CI jobs that want to distinguish "the safety layer's specific threats"
# from "the rest of the suite".
#
# `test-security` used to point at `tests/security/`, which is a
# directory that does not exist in this project — pytest exits with
# code 5 ("no tests collected"), which the `|| true`-free `check` chain
# would have caught if the target were actually wired in. It is
# corrected here to point at `tests/adversarial/`, which is the
# security-relevant test surface.
test: ## Run the full test suite with coverage (the coverage gate is enforced here)
	uv run pytest

test-unit: ## Run only unit tests
	uv run pytest tests/unit -m unit

test-integration: ## Run only integration tests
	uv run pytest tests/integration -m integration

test-contract: ## Run only contract tests
	uv run pytest tests/contract -m contract

test-e2e: ## Run only end-to-end tests
	uv run pytest tests/e2e -m e2e

test-security: ## Run only security tests (the adversarial suite)
	uv run pytest tests/adversarial -v

test-file: ## Run a single test file without the coverage gate (usage: make test-file FILE=tests/unit/core/test_version.py)
	@if [ -z "$(FILE)" ]; then \
		echo "Error: 'FILE' is required." >&2; \
		echo "Usage: make test-file FILE=tests/unit/core/test_version.py" >&2; \
		exit 2; \
	fi
	uv run pytest "$(FILE)" -v --no-cov

coverage: ## Run tests and open the HTML coverage report
	uv run pytest --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# -----------------------------------------------------------------------------
# Security
# -----------------------------------------------------------------------------
# Bandit scans `src/` for common security anti-patterns. `pip-audit` scans
# the *exported* dependency set (not the editable install, which would
# trip `--strict` with a "distribution marked as editable" false positive).
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
# Pre-commit
# -----------------------------------------------------------------------------
# Runs the full pre-commit suite on all files. This is the same gate that
# runs on `git commit`, but applied to every file at once. Useful before
# opening a PR, or after a bulk edit, to catch anything the per-file hook
# might have missed.
pre-commit: ## Run pre-commit hooks on all files
	uv run pre-commit run --all-files

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
# Pydantic model generation
# -----------------------------------------------------------------------------
# `scripts/generate_models.py` regenerates `src/forge/models/blueprint.py`
# from `src/forge/schemas/blueprint.schema.json`. It is the anti-drift
# mechanism for the schema↔model parity test (see
# `tests/unit/models/test_schema_model_parity.py` and ADR-0008).
#
# `models` writes the regenerated file (run after editing the schema).
# `models-check` verifies the on-disk file matches what the script would
# produce — it exits 1 if there is a diff, which is what CI and
# `make check` use as the gate.
#
# The `PY` variable allows overriding the interpreter (e.g. `make models
# PY=python3`), and defaults to `uv run python` so the invocation is
# consistent with the rest of the Makefile.
PY ?= uv run python

models: ## Regenerate Pydantic models from the JSON Schema (writes changes)
	$(PY) scripts/generate_models.py
	@echo "✅ Models regenerated. Review with 'git diff' before committing."

models-check: ## Verify Pydantic models are in sync with the JSON Schema (CI gate)
	@if ! $(PY) scripts/generate_models.py --check; then \
		echo "" >&2; \
		echo "❌ Pydantic models are stale." >&2; \
		echo "   Run 'make models' to regenerate them, then re-stage and commit." >&2; \
		exit 1; \
	fi
	@echo "✅ Models in sync with schema."

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ .pytest_cache/ .mypy_cache/ .ruff_cache/ \
	       .coverage coverage.xml htmlcov/ .hypothesis/
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	@echo "✅ Cleaned."
