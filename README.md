<div align="center">

# Forge CLI

**The engineering foundation system for software projects.**

Create, verify, and maintain consistent software projects through reusable, versioned engineering blueprints.

[![CI](https://github.com/thapelomagqazana/forge-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/thapelomagqazana/forge-cli/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/thapelomagqazana/forge-cli/branch/main/graph/badge.svg)](https://codecov.io/gh/thapelomagqazana/forge-cli)
[![PyPI](https://img.shields.io/pypi/v/forge-cli.svg)](https://pypi.org/project/forge-cli/)
[![Python Versions](https://img.shields.io/pypi/pyversions/forge-cli.svg)](https://pypi.org/project/forge-cli/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

[Documentation](docs/) · [Quick Start](#quick-start) · [Blueprints](#available-blueprints) · [Contributing](CONTRIBUTING.md)

</div>

---

## The Problem

Every new software project begins with the same repeated work:

- Choosing a project structure.
- Configuring Python, Node, or Java tooling.
- Installing and pinning dependencies.
- Setting up testing, linting, and type-checking.
- Adding Docker, CI pipelines, and security checks.
- Writing a README, `.env.example`, and `.gitignore`.
- Documenting decisions that will be forgotten in three months.

A senior engineer knows how to do all of this. A junior engineer may not know which decisions to make. An enterprise team may have five competing ways of doing the same thing.

The result: **inconsistent project foundations, technical debt from day one, and time wasted rebuilding the same setup over and over.**

---

## The Solution

**Forge CLI** turns engineering knowledge into executable, reusable **blueprints**.

A blueprint is not just a folder of templates. It is a versioned, validated specification of a project's engineering foundation: its structure, its variables, its templates, its conditional files, and its verification checks.

With one command, you get a project that is **ready to build, test, and maintain** — not merely ready to edit.

```bash
forge create python.fastapi.api --name payments-api
cd payments-api
forge verify
```

That is the intended experience. In the current alpha, `forge list`, `forge validate`, **and `forge create --dry-run`** are complete, together with the engine components they rely on (blueprint discovery, sandboxed template rendering, variable resolution, deterministic file planning, sandboxed staging). The write path of `forge create` — staging, commit, backups, `--overwrite` — is complete at the *code* level and being finalized at the *test* level. `forge verify` remains a stub. The reasoning behind the alpha's shape is in [Where This Is Today](#where-this-is-today), and the roadmap is in [Status](#status).

---

## Where This Is Today

Forge CLI is under active development. The **command surface** — `list`, `create`, `validate`, `verify`, `doctor`, `ping` — is complete: every command is registered by the file-based registry, `forge --help` and `forge --version` work end-to-end, and each command's argument shapes, help text, exit-code contract, and `--json` envelope are already final and enforced by the test suite.

The **engine** that powers generation is also complete — each piece is a tested, bounded module, and the pipeline that composes them is now in place:

- **Blueprint discovery** — layered sources (bundled + local), deterministic precedence, structured warnings. [ADR 0009](docs/adr/0009-blueprint-discovery-and-precedence.md).
- **Blueprint loading** — safe YAML parse, schema + Pydantic validation, aggregated, actionable errors. [ADR 0010](docs/adr/0010-loader-result-and-error-aggregation.md).
- **Variable resolution** — flags > prompts > defaults, one validation funnel, `ResolvedValues` as the only type the renderer accepts. [ADR 0011](docs/adr/0011-variable-resolution-precedence-and-validation.md).
- **Sandboxed Jinja2 rendering** — allowlisted filters, hardened sandbox, deterministic output, structured `TemplateRenderError`. [ADR 0014](docs/adr/0014-sandboxed-deterministic-templates.md).
- **File planning** — pure, total, sorted `PlannedFile[]` with collision detection and path-safety. [ADR 0015](docs/adr/0015-planning-is-pure-and-total.md).
- **Filesystem safety layer** — one resolver, one policy, one staging discipline, backups for `--overwrite`, exit-code contract for safety refusals. [ADR 0017](docs/adr/0017-filesystem-safety-layer.md).
- **Dry-run plan builder** — pure `GenerationPlan` DTO, versioned JSON contract, deterministic output, no filesystem mutations. [ADR 0018](docs/adr/0018-dry-run-is-a-pure-plan-builder.md).

The **command bodies** that orchestrate these components are at different levels of completeness:

| Command | State today | Notes |
| :--- | :--- | :--- |
| `forge --version` | ✅ Complete | Reads from `importlib.metadata`, cannot drift from `pip show`. |
| `forge ping` | ✅ Complete | Smoke-test the CLI is installed and runnable. |
| `forge list` | ✅ Complete | Discovers bundled and local blueprints; renders a table or JSON. |
| `forge list --json` | ✅ Complete | Stable JSON envelope for scripting. |
| `forge list --category <c>` | ✅ Complete | Filters the catalogue by category. |
| `forge list --source <s>` | ✅ Complete | Filters the catalogue by layer (`official` or `local`). |
| `forge validate <path>` | ✅ Complete | Runs the full load pipeline (YAML → schema → model → files); emits human or `--json` reports. Exit `0` valid, `2` blueprint error. |
| `forge validate --json <path>` | ✅ Complete | Stable JSON envelope with `valid` and `errors[]`. |
| `forge create --dry-run` | ✅ Complete | Builds the full generation plan, renders it as human text or `--json`, exits `0` on clean plan and `7` on collisions. **Never writes.** |
| `forge create --dry-run --json` | ✅ Complete | Stable JSON envelope (versioned, deterministic, ANSI-free). |
| `forge create` | 🚧 In progress | The generation pipeline (plan → safety → backup → stage → commit) is implemented; test coverage for the write path is being finalized this iteration. |
| `forge create --overwrite` | 🚧 In progress | Backups-before-replace is implemented; test coverage is being finalized this iteration. |
| `forge verify` | 🚧 Stub | Emits `{"status": "not_implemented"}` in JSON mode. The verification engine is a later task. |
| `forge doctor` | 🚧 Stub | Emits `{"status": "not_implemented"}` in JSON mode. The diagnostics engine is a later task. |

The stubs are intentional: they are the *contract* that future implementations must satisfy, and the test suite already enforces that contract. A command that prints `TODO: verify` still exits `0` (or `4` per the verification contract), still accepts every documented flag, and still emits a valid `--json` envelope — so a script written against the *intended* contract does not break when the body lands.

The three official blueprints ship today: `forge list` shows them, `forge list --json` serialises them, and `forge list --source local` shows overrides under `.forge/blueprints/`. `forge validate` runs the full validation pipeline against any blueprint, including custom ones. `forge create --dry-run` builds a complete, inspectable plan against any blueprint and any destination — **without ever touching the filesystem.**

---

## Quick Start

### 1. Install Forge CLI

Forge CLI is distributed as a Python package. We recommend [**`uv`**](https://docs.astral.sh/uv/) for installation:

```bash
uv tool install forge-cli
```

Alternatives:

```bash
# With pipx
pipx install forge-cli

# With pip (not recommended — use a virtual environment)
pip install forge-cli
```

Verify the installation:

```bash
forge --version
# forge, version <resolved>
```

The version reported by `forge --version` is read from the installed package metadata (via `importlib.metadata`). It cannot drift from `pip show forge-cli`, because they read from the same source. See [ADR 0002](docs/adr/0002-use-importlib-metadata-for-version-resolution.md) for the design.

### 2. Discover Available Blueprints

```bash
forge list
```

```
Available blueprints (3)
  ○ python.automation.cli          1.0.0    [official] Python CLI Automation Tool
  ○ python.fastapi.api             1.0.0    [official] Python FastAPI API
  ○ typescript.frontend.react      1.0.0    [official] TypeScript React Frontend
```

Add your own blueprints by placing them under `.forge/blueprints/<id>/blueprint.yaml` in the current working directory. Local blueprints override official ones with the same `id`. Use `forge list --source local` to see only your overrides, or `forge list --json` for a machine-readable catalogue. The discovery rules are documented in [`docs/architecture/discovery.md`](docs/architecture/discovery.md) and recorded in [ADR 0009](docs/adr/0009-blueprint-discovery-and-precedence.md).

### 3. Validate a Blueprint

Before you ship a blueprint (or debug a broken one), run it through the same pipeline the CLI uses at load time:

```bash
forge validate .forge/blueprints/acme.internal.api
```

```
✓ Blueprint acme.internal.api v1.0.0 is valid.
```

A broken blueprint gets every error at once — with a path, a message, and a hint:

```
✗ 3 validation error(s) in .forge/blueprints/acme.internal.api:
  • version  (…/blueprint.yaml)
      String should match pattern '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)…'
      hint: Use full SemVer, e.g. '1.0.0' (not '1.0').
  • id  (…/blueprint.yaml)
      Field required
      hint: Provide a dotted lowercase identifier such as 'python.fastapi.api'.
  • category  (…/blueprint.yaml)
      Input should be 'api', 'automation', 'frontend', 'library', 'cli' or 'other'
      hint: Pick one of: api, automation, frontend, library, cli, other.
```

For CI, add `--json` for a stable envelope (`valid`, `errors[]`) and switch on the exit code (`0` valid, `2` invalid, `1` tool failure). The full contract is in [`docs/reference/CLI_VALIDATE_OUTPUT.md`](docs/reference/CLI_VALIDATE_OUTPUT.md) and [ADR 0013](docs/adr/0013-exit-codes-as-public-contract.md).

### 4. Preview a Project (dry-run)

**The safest way to see what `forge create` would do** — including collisions — without writing anything:

```bash
forge create python.fastapi.api \
    --name payments-api \
    --var project=payments-api \
    --no-interactive \
    --dry-run
```

```
Dry-run: python.fastapi.api v1.0.0
Destination: /home/user/projects/payments-api
Mode: no-overwrite

Would create 11 file(s), skip 0, find 0 collision(s), 0 warning(s).

Files to create:
  + .dockerignore  (copy; from templates/.dockerignore)
  + .gitignore  (copy; from templates/.gitignore)
  + Dockerfile  (render; from templates/Dockerfile.j2)
  + README.md  (render; from templates/README.md.j2)
  + compose.yaml  (render; from templates/compose.yaml.j2)
  + pyproject.toml  (render; from templates/pyproject.toml.j2)
  + src/payments_api/__init__.py  (copy; from templates/src/{{project}}/__init__.py)
  + src/payments_api/main.py  (render; from templates/src/{{project}}/main.py.j2)
  + src/payments_api/settings.py  (render; from templates/src/{{project}}/settings.py.j2)
  + tests/conftest.py  (copy; from templates/tests/conftest.py)
  + tests/test_health.py  (render; from templates/tests/test_health.py.j2)

No collisions, no warnings. Re-run without --dry-run to apply.
```

For CI, use `--json` for a versioned, deterministic envelope and switch on the exit code (`0` clean, `7` collisions, `1`/`2`/`3`/`5` for other error categories):

```bash
forge create python.fastapi.api \
    --name payments-api --var project=payments-api \
    --no-interactive --dry-run --json > plan.json
case $? in
  0) echo "plan is clean" ;;
  7) echo "collisions detected — inspect plan.json" ; exit 1 ;;
  *) echo "dry-run failed" ; exit 1 ;;
esac
```

The dry-run **never writes** to the destination. Filesystem state before and after every dry-run is byte-identical (verified by a fingerprint test). The full contract — schema, guarantees, exit codes, and the plan's shape — is in [`docs/reference/DRY_RUN_PLAN.md`](docs/reference/DRY_RUN_PLAN.md) and [ADR 0018](docs/adr/0018-dry-run-is-a-pure-plan-builder.md).

### 5. Create a Project

```bash
forge create python.fastapi.api \
    --name payments-api \
    --var project=payments-api \
    --no-interactive
```

> 🚧 **The `create` write path is being finalized this iteration.** The pipeline (plan → safety validation → backup → stage → commit) is implemented end-to-end and the plan-side contract is fully tested. The write path's integration tests are the current focus. If you run `create` today, run it against a scratch directory, or use `--dry-run` — which is fully supported and never writes.

When the write path is finalized, `forge create` will:

1. Load the `python.fastapi.api` blueprint (loading pipeline: complete).
2. Resolve and validate your inputs (resolver: complete).
3. Plan the file manifest with collision detection (planner: complete).
4. Validate the plan against the destination filesystem (safety layer: complete).
5. Back up existing files if `--overwrite` was passed (safety layer: complete).
6. Render all files into a **staging directory** and commit atomically (staging: complete).
7. Write a `.forge/manifest.json` recording the blueprint version and your configuration (later task).

Steps 1–6 are implemented. Step 7 is a later task.

### 6. Verify the Project

```bash
cd payments-api
forge verify
```

> 🚧 **The `verify` command body is a stub in the current alpha.** In `--json` mode, it emits `{"status": "not_implemented"}`. In human mode, it prints `TODO: verify .`. The *exit code contract* is final: `0` means verified, `4` means verification failed, `2` means blueprint error, `5` means internal error.

When complete, `forge verify` will run the blueprint's declared verification checks (required files, dependency manifests, lint, type-check, tests, build, Docker) and report a structured result.

### 7. Start Building

Your project will be a normal software project. Forge will not leave behind any runtime dependency, hidden scripts, or magic. It will generate code that belongs to you.

```bash
uv sync
uv run uvicorn payments_api.main:app --reload
```

---

## What Forge CLI Does (and Doesn't Do)

### It Does

- ✅ **Discover blueprints** from the wheel and from `.forge/blueprints/` — complete today.
- ✅ **Override official blueprints** with local ones, deterministically, with a warning when a collision occurs — complete today.
- ✅ **Render a catalogue** in human and JSON forms with stable field names and exit codes — complete today.
- ✅ **Reject malformed blueprints** with a warning that names the file, so the rest of the catalogue still loads — complete today.
- ✅ **Reject schema-invalid blueprints** with a JSON-Pointer error path — complete today.
- ✅ **Validate blueprints on demand** — `forge validate` runs the full load pipeline (YAML → schema → model → files) and reports every error with a path and hint — complete today.
- ✅ **Sandbox and render templates** — deterministic output, allowlisted filters, hardened Jinja2 sandbox, structured errors — complete today.
- ✅ **Plan file writes** — pure, total planner with collision detection and path-safety — complete today.
- ✅ **Resolve variables** with a documented precedence and one validation funnel — complete today.
- ✅ **Preview a generation** with `--dry-run` — complete plan, human or `--json`, **never writes**, exits `7` on collisions so CI can gate safely — complete today.
- ⏳ **Generate complete projects** — the pipeline (plan → safety → backup → stage → commit) is implemented; the write path's integration test coverage is being finalized this iteration.
- ⏳ **Verify the generated project** with explicit, structured checks. *(Verification engine: later task.)*
- ⏳ **Record project provenance** in `.forge/manifest.json`. *(Manifest generation: later task.)*

The command *surface* — argument shapes, help text, exit-code contracts, JSON envelopes — is complete today and enforced by the test suite. The engine *components* that power generation are complete and independently tested. The **dry-run pipeline is fully complete** — it exercises every engine component from start to finish, without writing. The write path composes the same components plus the staging/commit step; its integration test coverage is the current work item.

### It Does Not

- ❌ Install your project's dependencies (use `uv`, `npm`, etc.).
- ❌ Replace Git, your package manager, or your CI system.
- ❌ Execute arbitrary code from downloaded blueprints.
- ❌ Modify your project after generation without your explicit consent.
- ❌ Require a Forge account, a cloud service, or a network connection.

> **The core promise:** *Create a project that is ready to build, test, and maintain — not merely ready to edit.*

---

## Available Blueprints

Three official blueprints ship with Forge CLI today. Each is validated against the canonical JSON schema before being bundled, and the wheel-level integration test proves they are discoverable from a clean install.

| ID | Name | Category | What the manifest currently declares |
| :--- | :--- | :--- | :--- |
| `python.fastapi.api` | Python FastAPI API | `api` | FastAPI app with a health endpoint, typed settings via `pydantic-settings`, pytest + httpx test harness, optional Dockerfile and compose file. |
| `python.automation.cli` | Python CLI Automation Tool | `cli` | Typer-based CLI with typed settings, structured logging (JSON/console/plain), optional shell completion, and pytest + `CliRunner` tests. |
| `typescript.frontend.react` | TypeScript React Frontend | `frontend` | Vite + React + TypeScript app with strict `tsconfig`, optional React Router, optional Tailwind/CSS Modules, optional vitest + Testing Library, optional ESLint + Prettier. |

Each blueprint is a **versioned, validated specification** and is designed to pass a clean-environment verification gate before each release:

1. Generate the project.
2. Install dependencies.
3. Run linting, type-checking, and tests.
4. Build the project.
5. Start the application.
6. Run smoke tests.
7. Publish the blueprint.

If any step fails, the blueprint is not released. Steps 2, 3, and 4 are already exercised by the blueprint verification contracts in the manifests; steps 1, 5, and 6 will run once the write and verification engines are fully integrated.

**Roadmap blueprints** (not shipped today): a full-stack FastAPI blueprint with SQLAlchemy, Alembic, and PostgreSQL; a Playwright-based test-automation blueprint; a React blueprint with Playwright E2E. The three shipped blueprints cover the same categories; the aspirational ones add specific toolchains that will ship as separate blueprints so a user can choose.

---

## Core Concepts

### Blueprints

A **blueprint** is a versioned, declarative specification of a project foundation. It defines:

- **Metadata:** ID, name, version, category, description, optional maintainer.
- **Variables:** Typed inputs (`string`, `boolean`, `enum`) with defaults, validation, and descriptions.
- **Files:** Jinja2 templates with conditional inclusion rules.
- **Verification:** A contract of checks to run against the generated project.
- **Compatibility:** The minimum Forge CLI version required to consume the blueprint.

Blueprints are validated against a **versioned JSON Schema** (`blueprint.schema.json`) and a matching set of Pydantic models before use. Invalid blueprints are rejected with actionable, path-aware errors — the schema is the referee ([ADR 0007](docs/adr/0007-blueprint-schema-as-canonical-contract.md)) and the models are a parity-tested projection of it ([ADR 0008](docs/adr/0008-schema-generated-pydantic-models.md)).

### Rendering

Templates are treated as **untrusted content**. Every render goes through a single sandboxed environment that:

- Uses `StrictUndefined` — a missing variable is an error with a name and a hint, never an empty string in the generated code.
- Exposes a **documented filter allowlist** — no I/O, no time, no randomness. The list is in [`docs/reference/TEMPLATE_FILTERS.md`](docs/reference/TEMPLATE_FILTERS.md) and kept in sync with the code by a doc-sync test.
- Denies known escape routes (`__class__`, `__globals__`, `__import__`, `__subclasses__`, …) as defense in depth on top of Jinja's sandbox.
- Produces **deterministic output**: same template + same variables ⇒ byte-identical result.

Errors are data (`TemplateRenderError` with path, line, column, and hint), not tracebacks. The design is recorded in [ADR 0014](docs/adr/0014-sandboxed-deterministic-templates.md), and the threat model lives in [`docs/security/TEMPLATE_SANDBOX.md`](docs/security/TEMPLATE_SANDBOX.md).

### Planning

Before anything is written, the blueprint is turned into a **manifest of planned files** — a stable, sorted `list[PlannedFile]` where each entry declares source, resolved destination, and kind (`render` or `copy`). The planner:

- Resolves destination paths through the same sandboxed renderer used for file bodies, so path substitution cannot become an escape hatch.
- Rejects empty substitutions, absolute paths, `..`, empty segments, and NUL bytes with actionable hints.
- Detects **collisions on the normalised destination** and reports every colliding source in a single error — never last-writer-wins.
- Is a pure, total function: no I/O, no rendering of file bodies, deterministic output.

The design is recorded in [ADR 0015](docs/adr/0015-planning-is-pure-and-total.md), and the destination rules are documented in [`docs/reference/PLANNING.md`](docs/reference/PLANNING.md).

### Dry-Run

`--dry-run` is a **full, inspectable preview** of what `create` would do. It:

- Builds the complete plan (files to create, files skipped by conditions, collisions, warnings).
- Renders it as human text or as a versioned JSON envelope.
- Exits `7` (not `6`) when the plan contains collisions, so CI can gate on "would this conflict?" without ever risking a write.
- **Never writes** — the destination is byte-identical before and after, verified by a fingerprint test.

The distinction between exit `6` (write refused) and exit `7` (dry-run found collisions) is deliberate and load-bearing: `6` means "retry with `--overwrite`"; `7` means "inspect the plan, then decide." The design is recorded in [ADR 0018](docs/adr/0018-dry-run-is-a-pure-plan-builder.md), and the JSON contract is in [`docs/reference/DRY_RUN_PLAN.md`](docs/reference/DRY_RUN_PLAN.md).

### Manifests

Every generated project will contain a `.forge/manifest.json` file (roadmap — this is a later task):

```json
{
  "manifest_schema_version": "1.0.0",
  "project": {
    "id": "0190f3c8-7c12-7b41-8d55-8e8a0f7d2c10",
    "name": "payments-api",
    "created_at": "2026-11-06T12:00:00Z"
  },
  "forge": { "version": "<resolved>" },
  "blueprint": {
    "id": "python.fastapi.api",
    "version": "1.0.0",
    "source": "official"
  },
  "configuration": {
    "author": "forge",
    "python_version": "3.12",
    "include_docker": true
  },
  "verification": {
    "expected_baseline_checks": ["has_pyproject", "has_package", "imports_cleanly", "tests_pass"]
  }
}
```

The manifest will be **readable without Forge installed**. It is the foundation for future lifecycle features like drift detection and safe updates.

### Verification

When the verification engine lands, `forge verify` will run the checks declared by the blueprint. Each check will have a **stable ID**, a **status** (`PASSED`, `FAILED`, `SKIPPED`, `NOT_AVAILABLE`, `NOT_RUN`, `ERROR`), a duration, and (on failure) an actionable diagnostic.

Run a single check with `--check`:

```bash
forge verify --check tests
```

Get machine-readable output with `--json`:

```bash
forge verify --json > report.json
```

> 🚧 The command body is a stub today. The `--check` and `--json` flags are declared and their contracts are documented, but the implementation lands in a later task.

### Safety Model

Forge CLI takes filesystem safety seriously:

| Guarantee | How it is (or will be) enforced |
| :--- | :--- |
| **No silent overwrites** | Generation fails on collision unless `--overwrite` is explicitly passed. Collision detection is complete today at the planning level; the write path's collision-refusal is complete at the code level and being finalized at the test level. |
| **No partial projects** | Files are rendered into a staging directory, validated, then committed atomically. Complete at the code level. |
| **No path traversal** | Destination and source paths are resolved through one safety net; absolute paths, `..`, empty segments, and NUL bytes are rejected before any write. Complete today. |
| **No arbitrary code execution** | Templates render in a hardened Jinja2 sandbox; filters are allowlisted; escape routes are denied. Complete today. |
| **Deterministic rendering** | No time, no randomness, sorted context — same inputs produce byte-identical output. Complete today. |
| **Dry-run never writes** | Two mechanisms: the plan builder never calls a writer (structural), and a fingerprint test verifies the destination tree is byte-identical before and after every dry-run. Complete today. |
| **Rollback on failure** | A failed commit triggers automatic rollback to the pre-generation state. Backups-before-replace are implemented; full transactional rollback is a later task. |

See [`docs/security/`](docs/security/) for details.

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `forge list` | List all available blueprints. |
| `forge list --json` | Emit the catalogue as JSON (stable for scripts). |
| `forge list --category <c>` | Only list blueprints in the given category. |
| `forge list --source <s>` | Only list blueprints from `official` or `local`. |
| `forge create <blueprint> --name <name>` | Create a new project from a blueprint. |
| `forge create <blueprint> --name <name> --dry-run` | Preview what create would do, without writing. |
| `forge create <blueprint> --name <name> --dry-run --json` | Preview as a versioned JSON envelope for scripting/CI. |
| `forge create <blueprint> --name <name> --overwrite` | Replace existing files, taking a timestamped backup of each first. |
| `forge validate <path>` | Validate a blueprint definition without generating a project. |
| `forge validate --json <path>` | Emit the validation result as JSON. |
| `forge verify` | Run the project's verification baseline. |
| `forge verify --check <id>` | Run a single verification check. |
| `forge verify --json` | Output verification results as JSON. |
| `forge doctor` | Diagnose your environment and project health. |
| `forge --version` | Show the installed version. |
| `forge --help` | Show help for any command. |

Full documentation for each command is available in [`docs/commands/`](docs/commands/).

### Exit Codes

| Code | Meaning |
| :--- | :--- |
| `0` | Success. |
| `1` | User input error (e.g., invalid project name, cancelled prompt) **or** an internal tool failure (`forge validate` distinguishes these on stderr). |
| `2` | Blueprint error (missing, malformed, schema-invalid, or incompatible with the installed Forge). `forge validate` uses this for *invalid* blueprints so CI can tell "the input is broken" from "the tool is broken". |
| `3` | Filesystem conflict (e.g., path escapes permitted root, unwritable parent). `--overwrite` cannot help here. |
| `4` | Verification failed (one or more required checks did not pass) **or** a template rendering error. |
| `5` | Internal error (unexpected failure — a Forge bug, please report). |
| `6` | **Write**-path collision — the target exists and `--overwrite` was not passed. The write was refused; nothing was modified. |
| `7` | **Dry-run** collision — the plan was produced and contains collisions. **Nothing was written.** Inspect the plan, then decide. |

The distinction between `6` and `7` is deliberate: `6` means "retry with `--overwrite` or choose a different destination"; `7` means "inspect the plan on stdout, then decide." A CI script that gates on `7` never risks a write; a script that saw `6` could not.

Stable exit codes make Forge CLI safe to use in CI pipelines and scripts. The codes are documented in the help text of every command, so `forge <command> --help` is the canonical reference. The full contract, including which exception types map to which codes, is in [`docs/reference/exit-codes.md`](docs/reference/exit-codes.md) and [ADR 0005](docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md) (with the `forge validate` refinement in [ADR 0013](docs/adr/0013-exit-codes-as-public-contract.md) and the dry-run distinction in [ADR 0018](docs/adr/0018-dry-run-is-a-pure-plan-builder.md)).

---

## Example Workflows

### Discover and Create a Python API

```bash
forge list
forge list --json | jq '.blueprints[].id'

# Preview the generation without writing anything.
forge create python.fastapi.api \
    --name payments-api \
    --var project=payments-api \
    --no-interactive \
    --dry-run

# When the write path is finalized, drop --dry-run to apply:
forge create python.fastapi.api --name payments-api --var project=payments-api --no-interactive
cd payments-api
uv sync
uv run uvicorn payments_api.main:app --reload
forge verify
```

### Preview Generation in CI (gating on collisions)

```bash
forge create python.fastapi.api \
    --name payments-api \
    --var project=payments-api \
    --no-interactive \
    --dry-run --json > plan.json
case $? in
  0) echo "plan is clean" ;;
  7) echo "collisions detected — inspect plan.json" ; exit 1 ;;
  *) echo "dry-run failed" ; exit 1 ;;
esac
```

### Validate a Custom Blueprint

```bash
mkdir -p .forge/blueprints/acme.internal.api
$EDITOR .forge/blueprints/acme.internal.api/blueprint.yaml

forge validate .forge/blueprints/acme.internal.api
# ✓ Blueprint acme.internal.api v1.0.0 is valid.

# In CI, fail on a broken blueprint without parsing prose:
forge validate --json .forge/blueprints/acme.internal.api > result.json
case $? in
  0) echo "blueprint ok" ;;
  2) echo "blueprint invalid — see result.json"; exit 1 ;;
  1) echo "tool error"; exit 1 ;;
esac
```

### Create a TypeScript Frontend

```bash
forge create typescript.frontend.react --name customer-portal
cd customer-portal
npm install
npm run dev
forge verify
```

### Create a CLI Automation Tool

```bash
forge create python.automation.cli --name ops-tool
cd ops-tool
uv sync
uv run ops-tool --help
forge verify
```

### Add a Custom Blueprint

```bash
mkdir -p .forge/blueprints/acme.internal.api
$EDITOR .forge/blueprints/acme.internal.api/blueprint.yaml

forge list
# Acme Internal API will appear with source [local].
# It will override any official blueprint with the same id.
```

### Use in CI (Non-Interactive)

```bash
forge list --json > catalogue.json
forge validate --json .forge/blueprints/acme.internal.api > validation.json
forge create python.fastapi.api --name payments-api --var project=payments-api --no-interactive --dry-run --json > plan.json
cd payments-api
forge verify --json > verification-report.json
```

---

## Configuration

Forge CLI reads configuration from (in order of precedence):

1. **Command-line flags** (highest priority).
2. **Environment variables** (prefixed with `FORGE_`).
3. **Project configuration** (`.forge/config.yaml`).
4. **User configuration** (`~/.config/forge/config.yaml`).
5. **Built-in defaults** (lowest priority).

Example user configuration:

```yaml
# ~/.config/forge/config.yaml
schema_version: "1.0"
defaults:
  registry: official
  include_tests: true
preferences:
  interactive: true
  colour: auto
```

> 🚧 Configuration loading is a later task. Today the CLI reads no configuration beyond the standard environment variables used by Rich (`NO_COLOR`, `FORCE_COLOR`) and `importlib.metadata` for version resolution.

---

## Troubleshooting

### `forge: command not found`

The installation directory is not on your `PATH`. With `uv`, ensure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add this line to your `~/.bashrc` or `~/.zshrc` to make it permanent.

### `forge list` shows no blueprints

The wheel should ship three official blueprints. If `forge list` shows zero, the installation is broken — the bundled resources are missing. Run `pip show -f forge-cli | grep blueprints` to check; if you see nothing, reinstall with `uv tool install --force forge-cli`. This is a packaging bug; please report it with your Python version and how you installed Forge.

### `Target directory already exists`

Forge CLI refuses to overwrite existing files by default. Use a different `--name` or `--output`, or pass `--overwrite` if you are certain you want to replace the existing files (a timestamped backup is created for each). To preview what would be overwritten first, run with `--dry-run --json` and inspect the `collisions[]` array.

### `Blueprint validation failed`

A blueprint definition is malformed. Run `forge validate <path>` for a detailed, path-aware error report. Every error is reported with a JSON-Pointer-style path, a message, and a hint; you can fix every problem in one pass. In CI, use `forge validate --json <path>` and switch on the exit code (`0` valid, `2` invalid, `1` tool error).

### `undefined variable: project` (or similar) during dry-run

The blueprint's file destinations reference a template variable the blueprint does not declare in `variables:[]`. Supply it explicitly with `--var project=<name>` — the flag is merged into the plan's variable mapping even for undeclared keys, so template-only variables work. If you own the blueprint, consider declaring the variable in its `variables:` list so it appears in `forge validate`'s report.

For more troubleshooting, see [`docs/commands/`](docs/commands/) or [open an issue](https://github.com/thapelomagqazana/forge-cli/issues/new?template=bug_report.yml).

---

## Status

🚧 **Alpha (v0.1.0a0)**

Forge CLI is under active development. The command surface is complete, `forge list`, `forge validate`, and **`forge create --dry-run`** are fully functional, and every command is registered by the file-based registry. The engine components that power generation — discovery, loading, variable resolution, sandboxed rendering, file planning, safety, dry-run — are complete and independently tested. The write path of `forge create` is implemented end-to-end and being finalized at the test level. The remaining work is the verification, diagnostics, and configuration engines, plus manifest generation and end-to-end integration coverage of the write path. The product is **not yet recommended for production use**.

The roadmap beyond the alpha includes:

- **Write-path finalization** — completing the integration test coverage of `forge create` (non-dry-run), including `--overwrite` backups and end-to-end staging/commit.
- **Manifest generation** — writing `.forge/manifest.json` during `create` so future lifecycle features (drift detection, safe updates) can read project provenance.
- **Verification engine** — manifest loading, blueprint loading, check registry, check handlers, result aggregation, summary rendering.
- **Diagnostics engine** — environment checks, manifest validation, project structure checks, hint rendering.
- **Blueprint library** — expand the three launch blueprints and pass each through a clean-environment verification gate.
- **Drift detection** — compare a project against its blueprint baseline.
- **Safe updates** — apply blueprint changes without losing user edits.
- **Shared team blueprints** — distribute approved foundations across repositories.
- **Private registry** — a controlled distribution system for enterprise blueprints.
- **Engineering governance** — enforce standards across teams and repositories.
- **Platform integrations** — CI/CD, developer portals, and repository workflows.

See [CHANGELOG.md](CHANGELOG.md) for release history and [`docs/adr/`](docs/adr/) for the architecture decision records.

---

## Contributing

We welcome contributions of all kinds: bug reports, feature requests, documentation improvements, and code.

### Quick Start for Contributors

```bash
git clone https://github.com/thapelomagqazana/forge-cli.git
cd forge-cli
make install        # uv sync --all-extras + pre-commit install
make check          # run the full quality gate locally
```

### Common Commands

| Task | Command |
| :--- | :--- |
| Show all commands | `make` |
| Run the full quality gate | `make check` |
| Run all tests | `make test` |
| Run one test file (fast, no coverage gate) | `make test-file FILE=tests/unit/core/test_version.py` |
| Auto-fix lint and format | `make fix` |
| Regenerate the Pydantic models | `make models` |
| Fail if models drift from the schema | `make models-check` |
| Create a new ADR | `make adr title="Your decision"` |
| Rebuild the ADR index | `make adr-index` |

### Read the Guide

- **Read the guide:** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Commit convention:** [docs/contributing/commit-convention.md](docs/contributing/commit-convention.md)
- **Report a bug:** [Bug Report Template](https://github.com/thapelomagqazana/forge-cli/issues/new?template=bug_report.yml)
- **Request a blueprint:** [Blueprint Request Template](https://github.com/thapelomagqazana/forge-cli/issues/new?template=blueprint_request.yml)
- **Report a security issue:** See [SECURITY.md](SECURITY.md)

By participating, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## License

Forge CLI is released under the [**Apache License 2.0**](LICENSE), a permissive license that includes an express patent grant — making it a good fit for developer tooling.

---

<div align="center">

**Stop rebuilding the foundation. Start building the product.**

[Get Started](#quick-start) · [GitHub](https://github.com/thapelomagqazana/forge-cli) · [Issues](https://github.com/thapelomagqazana/forge-cli/issues)

</div>
