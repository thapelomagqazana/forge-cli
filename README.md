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

That is the intended experience. In the current alpha, `forge list` is complete and discovers the three official blueprints; `forge create` echoes the arguments you passed; `forge verify` reports `not_implemented`. The reasoning behind the alpha's shape is in [Where This Is Today](#where-this-is-today), and the roadmap is in [Status](#status).

---

## Where This Is Today

Forge CLI is under active development. The **command surface** — `list`, `create`, `validate`, `verify`, `doctor`, `ping` — is complete: every command is registered by the file-based registry, `forge --help` and `forge --version` work end-to-end, and each command's argument shapes, help text, exit-code contract, and `--json` envelope are already final and enforced by the test suite.

The **command bodies** are at different levels of completeness:

| Command | State today | Notes |
| :--- | :--- | :--- |
| `forge --version` | ✅ Complete | Reads from `importlib.metadata`, cannot drift from `pip show`. |
| `forge ping` | ✅ Complete | Smoke-test the CLI is installed and runnable. |
| `forge list` | ✅ Complete | Discovers bundled and local blueprints; renders a table or JSON. |
| `forge list --json` | ✅ Complete | Stable JSON envelope for scripting. |
| `forge list --category <c>` | ✅ Complete | Filters the catalogue by category. |
| `forge list --source <s>` | ✅ Complete | Filters the catalogue by layer (`official` or `local`). |
| `forge create` | 🚧 Stub | Echoes the arguments you passed. The generation pipeline is a later task. |
| `forge validate` | 🚧 Stub | Echoes the path. Schema validation is a later task. |
| `forge verify` | 🚧 Stub | Emits `{"status": "not_implemented"}` in JSON mode. The verification engine is a later task. |
| `forge doctor` | 🚧 Stub | Emits `{"status": "not_implemented"}` in JSON mode. The diagnostics engine is a later task. |

The stubs are intentional: they are the *contract* that future implementations must satisfy, and the test suite already enforces that contract. A command that prints `TODO: list blueprints` still exits `0`, still accepts every documented flag, and still emits a valid `--json` envelope — so a script written against the *intended* contract does not break when the body lands.

The full generation pipeline, verification engine, and diagnostics engine land in later tasks. The three official blueprints ship today: `forge list` shows them, `forge list --json` serialises them, and `forge list --source local` shows overrides under `.forge/blueprints/`.

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

### 3. Create a Project

```bash
forge create python.fastapi.api --name payments-api
```

> 🚧 **The `create` command body is a stub in the current alpha.** It echoes the arguments you passed. The generation pipeline lands in a later task. The *argument contract* is final: `--name` is required, `--output` defaults to `.`, `--dry-run` and `--overwrite` are accepted, and the help text documents every exit code.

When complete, `forge create` will:

1. Load the `python.fastapi.api` blueprint.
2. Resolve and validate your inputs.
3. Render all files into a **staging directory** (never directly into your workspace).
4. Validate the staged output (required files, manifest, path safety).
5. Commit the project atomically to `./payments-api/`.
6. Write a `.forge/manifest.json` recording the blueprint version and your configuration.

### 4. Verify the Project

```bash
cd payments-api
forge verify
```

> 🚧 **The `verify` command body is a stub in the current alpha.** In `--json` mode, it emits `{"status": "not_implemented"}`. In human mode, it prints `TODO: verify .`. The *exit code contract* is final: `0` means verified, `4` means verification failed, `2` means blueprint error, `5` means internal error.

When complete, `forge verify` will run the blueprint's declared verification checks (required files, dependency manifests, lint, type-check, tests, build, Docker) and report a structured result.

### 5. Start Building

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
- ✅ **Reject schema-invalid blueprints** with a JSON-Pointer error path — complete today (validated at discovery time).
- ⏳ **Generate complete projects** from versioned, validated blueprints. *(Generation pipeline: later task.)*
- ⏳ **Preview before applying** with `--dry-run` and a detailed generation plan. *(Later task.)*
- ⏳ **Verify the generated project** with explicit, structured checks. *(Verification engine: later task.)*
- ⏳ **Record project provenance** in `.forge/manifest.json`. *(Manifest generation: later task.)*
- ⏳ **Stage and roll back** — a failed generation leaves no partial project. *(Transaction engine: later task.)*

The command *surface* — argument shapes, help text, exit-code contracts, JSON envelopes — is complete today and enforced by the test suite. The command *bodies* are the remaining work. The blueprints are complete today: three ship in the wheel, and `forge list` renders them.

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

If any step fails, the blueprint is not released. The generation pipeline that would run steps 1, 5, and 6 is a later task; steps 2, 3, and 4 are already exercised by the blueprint verification contracts in the manifests.

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

Blueprints are validated against a **versioned JSON Schema** (`blueprint.schema.json`) and a matching set of Pydantic models before use. Invalid blueprints are rejected with actionable, path-aware errors — the schema is the referee (ADR-0007) and the models are a parity-tested projection of it (ADR-0008).

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

| Guarantee | How it will be enforced |
| :--- | :--- |
| **No silent overwrites** | Generation fails on collision unless `--overwrite` is explicitly passed. |
| **No partial projects** | Files are rendered into a staging directory, validated, then committed atomically. |
| **No path traversal** | All paths are canonicalized and validated against the destination root. |
| **No arbitrary code execution** | Blueprint hooks are disabled by default; commands require explicit consent. |
| **Rollback on failure** | A failed commit triggers automatic rollback to the pre-generation state. |

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
| `forge validate <path>` | Validate a blueprint definition without generating a project. |
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
| `1` | User input error (e.g., invalid project name, cancelled prompt). |
| `2` | Blueprint error (missing, malformed, schema-invalid, or incompatible with the installed Forge). |
| `3` | Filesystem conflict (e.g., target directory exists, path escapes permitted root). |
| `4` | Verification failed (one or more required checks did not pass). |
| `5` | Internal error (unexpected failure — a Forge bug, please report). |

Stable exit codes make Forge CLI safe to use in CI pipelines and scripts. The codes are documented in the help text of every command, so `forge <command> --help` is the canonical reference. The full contract, including which exception types map to which codes, is in [`docs/reference/exit-codes.md`](docs/reference/exit-codes.md) and [ADR 0005](docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md).

---

## Example Workflows

### Discover and Create a Python API

```bash
forge list
forge list --json | jq '.[].id'

# The generation pipeline is a later task; today `forge create` echoes
# the arguments you passed, so this shows the contract, not the output.
forge create python.fastapi.api --name payments-api
```

Once generation lands:

```bash
forge create python.fastapi.api --name payments-api
cd payments-api
uv sync
uv run uvicorn payments_api.main:app --reload
forge verify
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
forge create python.fastapi.api --name payments-api --no-interactive
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

### `Target directory already exists` (once generation lands)

Forge CLI refuses to overwrite existing directories by default. Use a different `--name` or `--output`, or pass `--overwrite` if you are certain you want to replace the directory.

### `Blueprint validation failed`

A blueprint definition is malformed. When the validator lands, run `forge validate <path>` for a detailed, path-aware error report. Today, malformed local blueprints are skipped with a warning at discovery time and are not shown in `forge list`.

For more troubleshooting, see [`docs/commands/`](docs/commands/) or [open an issue](https://github.com/thapelomagqazana/forge-cli/issues/new?template=bug_report.yml).

---

## Status

🚧 **Alpha (v0.1.0a0)**

Forge CLI is under active development. The command surface is complete, `forge list` is functional and discovers the three official blueprints, and every command is registered by the file-based registry. The remaining command bodies (generation, verification, diagnostics, configuration) will be filled in by later tasks. The product is **not yet recommended for production use**.

The roadmap beyond the alpha includes:

- **Generation pipeline** — blueprint loading, variable resolution, dry-run, staging, commit, manifest generation, summary rendering.
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
