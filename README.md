<div align="center">

# Forge CLI

**The engineering foundation system for software projects.**

Create, verify, and maintain consistent software projects through reusable, versioned engineering blueprints.

[![CI](https://github.com/your-org/forge-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/forge-cli/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/your-org/forge-cli/branch/main/graph/badge.svg)](https://codecov.io/gh/your-org/forge-cli)
[![PyPI](https://img.shields.io/pypi/v/forge-cli.svg)](https://pypi.org/project/forge-cli/)
[![Python Versions](https://img.shields.io/pypi/pyversions/forge-cli.svg)](https://pypi.org/project/forge-cli/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

[Documentation](https://forge-cli.dev) · [Quick Start](#quick-start) · [Blueprints](#available-blueprints) · [Contributing](CONTRIBUTING.md)

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

That is it. No manual setup. No forgotten configuration. No inconsistent standards.

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
# forge, version 0.1.0a0
```

### 2. Discover Available Blueprints

```bash
forge list
```

You should see:

```
ID                          Name                        Version   Category
python.fastapi.api          Python FastAPI API          1.0.0     backend
typescript.react.app        TypeScript React App        1.0.0     frontend
python.pytest.automation    Python Test Automation      1.0.0     automation
```

### 3. Create a Project

```bash
forge create python.fastapi.api --name payments-api
```

Forge will:

1. Load the `python.fastapi.api` blueprint.
2. Resolve and validate your inputs.
3. Render all files into a **staging directory** (never directly into your workspace).
4. Validate the staged output (required files, manifest, path safety).
5. Commit the project atomically to `./payments-api/`.
6. Write a `.forge/manifest.json` recording the blueprint version and your configuration.

You can preview everything first with `--dry-run`:

```bash
forge create python.fastapi.api --name payments-api --dry-run
```

### 4. Verify the Project

```bash
cd payments-api
forge verify
```

This runs the blueprint's declared verification checks (required files, dependency manifests, lint, type-check, tests, build, Docker) and reports a structured result:

```
Forge Verification — payments-api
Blueprint: python.fastapi.api@1.0.0

  ✓ required-files        PASSED   0.01s
  ✓ dependencies          PASSED   0.12s
  ✓ lint                  PASSED   1.84s
  ✓ typecheck             PASSED   2.13s
  ✓ tests                 PASSED   4.67s
  ✓ build                 PASSED   3.21s
  - docker                SKIPPED  (Docker not available)

Overall: PASSED  (5 passed, 1 skipped, 0 failed)
```

### 5. Start Building

Your project is now a normal software project. Forge did not leave behind any runtime dependency, hidden scripts, or magic. It generated code that belongs to you.

```bash
uv sync
uv run uvicorn payments_api.main:app --reload
```

---

## What Forge CLI Does (and Doesn't Do)

### It Does

- ✅ **Generate complete projects** from versioned, validated blueprints.
- ✅ **Preview before applying** with `--dry-run` and a detailed generation plan.
- ✅ **Verify the generated project** with explicit, structured checks.
- ✅ **Record project provenance** in `.forge/manifest.json`.
- ✅ **Protect existing files** — no silent overwrites, ever.
- ✅ **Stage and roll back** — a failed generation leaves no partial project.
- ✅ **Work offline** — blueprints are bundled with the CLI.
- ✅ **Support automation** — JSON output and stable exit codes.

### It Does Not

- ❌ Install your project's dependencies (use `uv`, `npm`, etc.).
- ❌ Replace Git, your package manager, or your CI system.
- ❌ Execute arbitrary code from downloaded blueprints.
- ❌ Modify your project after generation without your explicit consent.
- ❌ Require a Forge account, a cloud service, or a network connection.

> **The core promise:** *Create a project that is ready to build, test, and maintain — not merely ready to edit.*

---

## Available Blueprints

Forge CLI launches with three carefully selected, production-oriented blueprints. Quality matters more than quantity.

| ID | Name | Category | Description |
| :--- | :--- | :--- | :--- |
| `python.fastapi.api` | Python FastAPI API | Backend | FastAPI · Pydantic · SQLAlchemy · Alembic · PostgreSQL · Pytest · Docker · GitHub Actions |
| `typescript.react.app` | TypeScript React App | Frontend | React · TypeScript · Vite · Tailwind CSS · Vitest · Playwright · ESLint |
| `python.pytest.automation` | Python Test Automation | Automation | Pytest · Playwright · HTTPX · Pydantic · Allure-compatible reporting |

Each blueprint passes a **clean-environment verification gate** before release:

1. Generate the project.
2. Install dependencies.
3. Run linting, type-checking, and tests.
4. Build the project.
5. Start the application.
6. Run smoke tests.
7. Publish the blueprint.

If any step fails, the blueprint is not released.

---

## Core Concepts

### Blueprints

A **blueprint** is a versioned, declarative specification of a project foundation. It defines:

- **Metadata:** ID, name, version, category, description.
- **Variables:** Typed inputs (`string`, `boolean`, `enum`) with defaults, validation, and descriptions.
- **Files:** Jinja2 templates with conditional inclusion rules.
- **Verification:** A contract of checks to run against the generated project.

Blueprints are validated against a **versioned JSON Schema** (`blueprint.schema.json`) before use. Invalid blueprints are rejected with actionable, path-aware errors.

### Manifests

Every generated project contains a `.forge/manifest.json` file:

```json
{
  "manifest_schema_version": "1.0.0",
  "project": {
    "id": "0190f3c8-7c12-7b41-8d55-8e8a0f7d2c10",
    "name": "payments-api",
    "created_at": "2026-11-06T12:00:00Z"
  },
  "forge": { "version": "0.1.0a0" },
  "blueprint": {
    "id": "python.fastapi.api",
    "version": "1.0.0",
    "source": "official"
  },
  "configuration": {
    "database": "postgresql",
    "include_docker": true
  },
  "verification": {
    "expected_baseline_checks": ["required-files", "tests", "lint"]
  }
}
```

The manifest is **readable without Forge installed**. It is the foundation for future lifecycle features like drift detection and safe updates.

### Verification

`forge verify` runs the checks declared by the blueprint. Each check has a **stable ID**, a **status** (`PASSED`, `FAILED`, `SKIPPED`, `NOT_AVAILABLE`, `NOT_RUN`, `ERROR`), a duration, and (on failure) an actionable diagnostic.

```
Overall: FAILED
  ✓ required-files        PASSED   0.01s
  ✗ tests                 FAILED   2.14s
     2 tests failed. Run `pytest -v` for details.
  - docker                SKIPPED  (Docker not available)
```

Run a single check with `--check`:

```bash
forge verify --check tests
```

Get machine-readable output with `--json`:

```bash
forge verify --json > report.json
```

### Safety Model

Forge CLI takes filesystem safety seriously:

| Guarantee | How It Is Enforced |
| :--- | :--- |
| **No silent overwrites** | Generation fails on collision unless `--overwrite` is explicitly passed. |
| **No partial projects** | Files are rendered into a staging directory, validated, then committed atomically. |
| **No path traversal** | All paths are canonicalized and validated against the destination root. |
| **No arbitrary code execution** | Blueprint hooks are disabled by default; commands require explicit consent. |
| **Rollback on failure** | A failed commit triggers automatic rollback to the pre-generation state. |

See [`docs/security/safety-and-verification.md`](docs/security/safety-and-verification.md) for details.

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `forge list` | List all available blueprints. |
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
| `1` | User input error (e.g., invalid project name). |
| `2` | Blueprint error (e.g., schema validation failure). |
| `3` | Filesystem conflict (e.g., target directory exists). |
| `4` | Verification failed (one or more required checks did not pass). |
| `5` | Internal error (unexpected failure). |

Stable exit codes make Forge CLI safe to use in CI pipelines and scripts.

---

## Example Workflows

### Create a Python API

```bash
forge create python.fastapi.api --name payments-api
cd payments-api
uv sync
alembic upgrade head
uv run uvicorn payments_api.main:app --reload
forge verify
```

### Create a TypeScript Frontend

```bash
forge create typescript.react.app --name customer-portal
cd customer-portal
npm install
npm run dev
forge verify
```

### Create a Test Automation Project

```bash
forge create python.pytest.automation --name customer-portal-tests
cd customer-portal-tests
uv sync
playwright install
forge verify
```

### Use in CI (Non-Interactive)

```bash
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

---

## Troubleshooting

### `forge: command not found`

The installation directory is not on your `PATH`. With `uv`, ensure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add this line to your `~/.bashrc` or `~/.zshrc` to make it permanent.

### `Docker is not available` during `forge verify`

The Docker check is **optional**. If Docker is not installed, the check is reported as `SKIPPED` and does not cause the overall verification to fail. Install Docker to enable the check.

### `Target directory already exists`

Forge CLI refuses to overwrite existing directories by default. Use a different `--name` or `--output`, or pass `--overwrite` if you are certain you want to replace the directory.

### `Blueprint validation failed`

A blueprint definition is malformed. Run `forge validate <path>` for a detailed, path-aware error report.

For more troubleshooting, see [`docs/commands/`](docs/commands/) or [open an issue](https://github.com/your-org/forge-cli/issues/new?template=bug_report.yml).

---

## Status

🚧 **Alpha (v0.1.0a0)**

Forge CLI is under active development. The core workflow — `list`, `create`, `validate`, `verify`, `doctor` — is functional, but the product is **not yet recommended for production use**.

The roadmap beyond the alpha includes:

- **Drift detection** — compare a project against its blueprint baseline.
- **Safe updates** — apply blueprint changes without losing user edits.
- **Shared team blueprints** — distribute approved foundations across repositories.
- **Private registry** — a controlled distribution system for enterprise blueprints.
- **Engineering governance** — enforce standards across teams and repositories.
- **Platform integrations** — CI/CD, developer portals, and repository workflows.

See [CHANGELOG.md](CHANGELOG.md) for release history and [the roadmap](docs/architecture/roadmap.md) for what's next.

---

## Contributing

We welcome contributions of all kinds: bug reports, feature requests, documentation improvements, and code.

- **Read the guide:** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Report a bug:** [Bug Report Template](https://github.com/your-org/forge-cli/issues/new?template=bug_report.yml)
- **Request a blueprint:** [Blueprint Request Template](https://github.com/your-org/forge-cli/issues/new?template=blueprint_request.yml)
- **Report a security issue:** See [SECURITY.md](SECURITY.md)

By participating, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## License

Forge CLI is released under the [**Apache License 2.0**](LICENSE), a permissive license that includes an express patent grant — making it a good fit for developer tooling.

---

<div align="center">

**Stop rebuilding the foundation. Start building the product.**

[Get Started](#quick-start) · [Documentation](https://forge-cli.dev) · [GitHub](https://github.com/your-org/forge-cli)

</div>
