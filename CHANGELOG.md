# Changelog

All notable changes to this project will be documented in this file.

The format is based on [**Keep a Changelog**](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [**Semantic Versioning**](https://semver.org/spec/v2.0.0.html).

## Table of Contents

- [Unreleased](#unreleased)
- [0.1.0a0 — 2026-11-XX](#010a0--2026-11-xx)
- [Versioning Policy](#versioning-policy)
- [Changelog Categories](#changelog-categories)
- [How to Update This File](#how-to-update-this-file)

---

## [Unreleased]

### Added

- **CLI foundation** — `forge` command with `--help` and `--version` flags.
- **Command stubs** — `forge list`, `forge create`, `forge validate`, `forge verify`, `forge doctor`.
- **Blueprint schema v1** — JSON Schema (`blueprint.schema.json`) defining the structure of a Forge blueprint, including metadata, variables, files, conditional rules, and verification checks.
- **Pydantic models** — Typed models for blueprints, verification results, and generation results.
- **Blueprint loader** — Safe YAML parsing, JSON Schema validation, Pydantic validation, and error aggregation with location-aware diagnostics.
- **Template rendering engine** — Sandboxed Jinja2 environment with strict undefined variables, controlled filters, and deterministic rendering.
- **Filesystem safety layer** — Path canonicalization, path traversal prevention, collision detection, and staging-first generation.
- **Transactional generation** — Staging directory, pre-commit validation, atomic commit, and rollback on failure.
- **Project manifest** — `.forge/manifest.json` recording project identity, blueprint provenance, configuration, and expected baseline checks.
- **Verification engine** — Check registry, required-file check, dependency manifest check, secure command runner, lint/type-check handlers, test/build handlers, and Docker build check.
- **`forge verify` command** — Run the project's verification baseline with human-readable and JSON output.
- **`forge doctor` command** — Diagnose environment and project health (Python, uv, Node, Git, Docker, manifest).
- **Official blueprints** — Three production-oriented blueprints:
  - `python.fastapi.api` — FastAPI · Pydantic · SQLAlchemy · Alembic · PostgreSQL · Pytest · Docker · GitHub Actions.
  - `typescript.react.app` — React · TypeScript · Vite · Tailwind CSS · Vitest · Playwright · ESLint.
  - `python.pytest.automation` — Pytest · Playwright · HTTPX · Pydantic · Allure-compatible reporting.
- **JSON output mode** — `--json` flag on `list`, `create`, `validate`, `verify`, and `doctor`, with a versioned output envelope and per-command schemas.
- **Exit code enumeration** — Stable exit codes (`0`–`5`) for success, user error, blueprint error, filesystem conflict, verification failure, and internal error.
- **Configuration system** — Layered configuration with precedence: CLI flags > environment variables > project config > user config > defaults.
- **Security baseline** — `bandit` static analysis, `pip-audit` dependency auditing, and secret scanning in CI.
- **Documentation** — README, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `LICENSE`, command reference, blueprint reference, safety guide, and troubleshooting guide.
- **CI pipeline** — GitHub Actions matrix testing on Linux, Windows, and macOS with Python 3.12, linting (`ruff`), type-checking (`mypy --strict`), tests (`pytest`), and coverage enforcement (≥ 90%).
- **Release workflow** — PyPI Trusted Publishing via GitHub Actions OIDC, with versioned artifacts, changelog generation, and GitHub release creation.

### Changed

- _(nothing yet — this is the first release)_

### Deprecated

- _(nothing yet — this is the first release)_

### Removed

- _(nothing yet — this is the first release)_

### Fixed

- _(nothing yet — this is the first release)_

### Security

- Implemented path traversal protection and symlink escape detection.
- Implemented sandboxed Jinja2 rendering to prevent arbitrary code execution from templates.
- Implemented secure command runner with no shell interpolation, argument-list execution, working-directory restriction, environment allowlist, timeout enforcement, and output truncation.
- Implemented secret redaction for tokens, passwords, and credentials in logs, output, and diagnostic reports.
- No known vulnerabilities at the time of this release.

---

## [0.1.0a0] — 2026-11-XX

> **Status:** Alpha — Not recommended for production use.
>
> This is the first public alpha release of Forge CLI. The core workflow — `list`,
> `create`, `validate`, `verify`, `doctor` — is functional, but the product is
> under active development and may contain bugs. The alpha is intended for early
> adopters, design partners, and community feedback.

### Added

#### CLI Foundation

- `forge --help` — Display help for the root command and all subcommands.
- `forge --version` — Display the installed Forge CLI version (resolved dynamically from package metadata).
- `forge list` — List all discoverable blueprints (official and local).
- `forge create <blueprint> --name <name>` — Create a project from a blueprint.
  - Interactive wizard (default) — Guides the user through blueprint selection and variable configuration.
  - Non-interactive mode (`--no-interactive`) — For CI and scripting.
  - Dry-run mode (`--dry-run`) — Preview the generation plan without writing files.
  - Output directory (`--output`) — Specify a custom destination.
  - Overwrite flag (`--overwrite`) — Explicitly allow overwriting an existing directory.
- `forge validate <path>` — Validate a blueprint definition without generating a project.
- `forge verify` — Run the project's verification baseline.
  - Selective checks (`--check <id>`) — Run a single named check.
  - JSON output (`--json`) — Machine-readable verification report.
- `forge doctor` — Diagnose environment and project health.

#### Blueprint System

- **Blueprint schema v1** (`blueprint.schema.json`) — JSON Schema (Draft 2020-12) defining:
  - Metadata (`id`, `name`, `version`, `description`, `category`).
  - Variables (`type`, `description`, `default`, `required`, `validation`).
  - Files (source → destination mappings).
  - Conditional files (included only when a condition is true).
  - Verification contract (check IDs, types, commands, timeouts).
  - Forge compatibility (`min_forge_version`).
- **Pydantic models** — Typed models generated from and validated against the JSON Schema.
- **Blueprint loader** — Safe YAML parsing, schema validation, semantic validation, and error aggregation.
- **Blueprint discovery** — Discovers official blueprints from package resources and local blueprints from `.forge/blueprints/`.

#### Template Engine

- **Sandboxed Jinja2** — Restricted environment preventing arbitrary code execution.
- **Strict undefined variables** — Missing variables raise an error, not silently render empty.
- **Controlled filters** — Allowlist of safe filters (`upper`, `lower`, `default`, `replace`, `tojson`).
- **Deterministic rendering** — Same blueprint + variables = byte-identical output.
- **Variable substitution in paths** — Destination paths can contain template variables.
- **Literal file copying** — Binary files are copied byte-for-byte.
- **Conditional file inclusion** — Files are included only when a declared condition is true.

#### Project Generation

- **Staging-first generation** — Files are rendered into a temporary staging directory, validated, then committed atomically.
- **Path safety** — Canonicalization, path traversal prevention, symlink escape detection, and absolute path rejection.
- **Collision detection** — Fails on collision unless `--overwrite` is explicitly passed.
- **Transactional commit** — Atomic move from staging to destination, with cross-filesystem safety.
- **Rollback** — Failed generation restores the destination to its original state.
- **Project manifest** — `.forge/manifest.json` recording:
  - Manifest schema version.
  - Project identity (UUID, name, creation timestamp).
  - Forge version.
  - Blueprint provenance (ID, version, source).
  - Resolved configuration.
  - Expected baseline checks.

#### Verification Engine

- **Check registry** — Extensible registry mapping check types to handlers.
- **Required-file check** — Validates that blueprint-declared files exist.
- **Dependency manifest check** — Validates `pyproject.toml` / `package.json` and lockfiles.
- **Secure command runner** — Executes commands with:
  - No shell interpolation (`shell=False`).
  - Argument-list execution.
  - Working-directory restriction to the project root.
  - Environment allowlist (`PATH`, `HOME`, `LANG`, `PYTHONPATH`).
  - Timeout enforcement (default 60s).
  - Output truncation (10KB).
- **Lint and type-check handlers** — Ruff, Mypy, ESLint, and TypeScript.
- **Test and build handlers** — Pytest, Vitest, Playwright, Vite build, Python build.
- **Docker build check** — Validates that the Docker image builds (skipped if Docker is unavailable).
- **Status semantics** — Distinguishes `PASSED`, `FAILED`, `SKIPPED`, `NOT_AVAILABLE`, `NOT_RUN`, and `ERROR`.
- **Required vs. optional checks** — Only required checks affect the overall verification status.
- **Verification result schema** — Versioned JSON schema for verification reports.

#### JSON Output

- **Common envelope** — All JSON output follows a stable envelope: `{ "version": "1.0", "status": "success|error|partial", "data": {...}, "errors": [...], "warnings": [...] }`.
- **Per-command schemas** — Versioned JSON schemas for `list`, `create`, `validate`, `verify`, and `doctor`.
- **Stream separation** — JSON goes to `stdout`; human-readable messages go to `stderr`.
- **Error representation** — Errors include `code`, `message`, `hint`, and `path`.

#### Official Blueprints

- **`python.fastapi.api`** (v1.0.0) — Python API foundation:
  - FastAPI application factory.
  - Pydantic Settings configuration.
  - Structured JSON logging.
  - SQLAlchemy + Alembic migrations.
  - PostgreSQL configuration.
  - Sample CRUD route.
  - Pytest + HTTPX tests with in-memory SQLite.
  - Multi-stage Dockerfile with non-root user.
  - Docker Compose for local development.
  - GitHub Actions CI workflow.
  - Ruff + Mypy configuration.
  - Comprehensive README with troubleshooting.
- **`typescript.react.app`** (v1.0.0) — TypeScript frontend foundation:
  - React 18 + TypeScript + Vite.
  - React Router v6.
  - TanStack Query for server state.
  - Tailwind CSS for styling.
  - Reusable `Button` and `Card` components.
  - `Home` and `NotFound` pages.
  - Vitest + React Testing Library unit tests.
  - Playwright E2E tests.
  - ESLint (with `jsx-a11y`) + Prettier.
  - GitHub Actions CI workflow.
  - Comprehensive README.
- **`python.pytest.automation`** (v1.0.0) — Python test automation foundation:
  - Pytest configuration with markers (`smoke`, `api`, `ui`, `e2e`).
  - Playwright browser fixtures.
  - HTTPX API client wrapper.
  - Pydantic response models.
  - Test data management with Faker.
  - Page Object Model for UI tests.
  - HTML and Allure reporting.
  - Dockerfile for CI runs.
  - GitHub Actions CI workflow.
  - Comprehensive README with a guide for adding new tests.

#### Configuration

- **Layered configuration** with precedence:
  1. Command-line flags (highest).
  2. Environment variables (prefixed with `FORGE_`).
  3. Project configuration (`.forge/config.yaml`).
  4. User configuration (`~/.config/forge/config.yaml`).
  5. Built-in defaults (lowest).

#### Documentation

- `README.md` — Project overview, quick start, feature list, blueprint catalogue, limitations, and contribution guide.
- `CONTRIBUTING.md` — Development setup, commit convention, code style, testing requirements, PR process, and definition of done.
- `SECURITY.md` — Threat model, security controls, disclosure process, supported versions, and non-goals.
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1 with Forge-specific additions.
- `LICENSE` — Apache License 2.0.
- `CHANGELOG.md` — This file.
- `docs/` — MkDocs Material documentation site with:
  - Getting started guide.
  - Command reference (one page per command).
  - Blueprint reference (schema, variables, templates, conditions).
  - Safety and verification guide.
  - Architecture documentation (for contributors).
  - Security documentation.
  - Troubleshooting guide.

#### Testing

- **Unit tests** — Schema, loader, renderer, conditions, configuration.
- **Integration tests** — Blueprint generation, verification, offline generation.
- **Contract tests** — JSON schema compatibility, output contracts.
- **Security tests** — Path traversal, symlink escape, collision detection, rollback, command injection.
- **Property-based tests** — Hypothesis for generation invariants.
- **Cross-platform CI** — Linux, Windows, and macOS.
- **Coverage enforcement** — ≥ 90% overall, ≥ 95% for critical modules, 100% for security-critical modules.

#### CI/CD

- **GitHub Actions CI** — Matrix testing on Ubuntu, Windows, and macOS.
- **Linting** — `ruff check` + `ruff format --check`.
- **Type checking** — `mypy --strict`.
- **Testing** — `pytest` with coverage reporting.
- **Security scanning** — `bandit` + `pip-audit`.
- **Release workflow** — PyPI Trusted Publishing via GitHub Actions OIDC.
- **Documentation deployment** — MkDocs Material to GitHub Pages.

### Changed

- _(This is the first release; no changes to report.)_

### Deprecated

- _(This is the first release; nothing is deprecated.)_

### Removed

- _(This is the first release; nothing has been removed.)_

### Fixed

- _(This is the first release; no bug fixes to report.)_

### Security

- **Path traversal prevention** — All paths are canonicalized and validated against the destination root. Adversarial paths (`../../etc/passwd`, absolute paths, symlink escapes) are rejected.
- **Sandboxed template rendering** — Jinja2 templates run in a `SandboxedEnvironment` with a restricted filter allowlist. Arbitrary Python code cannot be executed from templates.
- **Secure command execution** — Verification commands are executed with `shell=False`, an argument list, a restricted working directory, an environment allowlist, a timeout, and output truncation. No shell interpolation is possible.
- **Secret redaction** — Known secret patterns (tokens, passwords, API keys) are redacted from logs, output, and diagnostic reports.
- **No secrets in manifests** — The `.forge/manifest.json` never contains credentials, tokens, or secret values.
- **Trusted publishing** — PyPI releases use GitHub Actions OIDC Trusted Publishing; no long-lived API tokens are used.
- **No known vulnerabilities** — At the time of this release, no known vulnerabilities have been reported.

### Known Limitations

This is an **alpha** release. The following limitations are known and documented:

- **No blueprint registry** — Blueprints must be local or bundled. A public/private registry is planned for a future phase.
- **No lifecycle management** — Forge CLI does not yet detect drift or apply safe updates to generated projects. Planned for a future phase.
- **No team blueprints** — Shared team configuration and private blueprint distribution are not yet supported.
- **No enterprise governance** — Policy evaluation and organisation-wide standards enforcement are not yet supported.
- **No plugin system** — Extensions are not yet supported.
- **Limited blueprint catalogue** — Only three official blueprints are available. More will be added based on community demand.
- **Docker check is optional** — If Docker is not installed, the `docker_build` check is reported as `SKIPPED` rather than `PASSED` or `FAILED`.
- **Windows path edge cases** — Some Windows-specific path edge cases (e.g., very long paths, UNC paths) may not be fully tested.
- **No interactive prompt timeout in all terminals** — Some terminals may not respect the 5-minute prompt timeout.

### Upgrade Notes

This is the first release, so there is no upgrade path from a prior version.

### Feedback

We want to hear from you! Please report bugs, request features, and share your
experience:

- **Bug reports:** [GitHub Issues — Bug Report](https://github.com/your-org/forge-cli/issues/new?template=bug_report.yml)
- **Blueprint requests:** [GitHub Issues — Blueprint Request](https://github.com/your-org/forge-cli/issues/new?template=blueprint_request.yml)
- **General feedback:** [GitHub Discussions](https://github.com/your-org/forge-cli/discussions)
- **Security issues:** [security@forge-cli.dev](mailto:security@forge-cli.dev)

---

## Versioning Policy

Forge CLI follows [**Semantic Versioning 2.0.0**](https://semver.org/spec/v2.0.0.html):

| Version | Meaning |
| :--- | :--- |
| `0.x.y` | Early development and public preview. Breaking changes may occur in minor versions. |
| `1.0.0` | Stable core CLI and documented compatibility. |
| `1.x.y` | Backward-compatible feature releases. |
| `2.0.0` | Deliberate breaking changes with migration guidance. |

### Pre-Release Suffixes

| Suffix | Meaning |
| :--- | :--- |
| `aN` | Alpha (e.g., `0.1.0a0`). Early preview, may be unstable. |
| `bN` | Beta (e.g., `0.2.0b1`). Feature-complete, undergoing validation. |
| `rcN` | Release candidate (e.g., `1.0.0rc1`). Final testing before stable. |

### Breaking Changes

A breaking change is any change that requires users to modify their workflows,
configuration, or generated projects. Examples:

- Renaming or removing a command or flag.
- Changing the exit code for an existing failure mode.
- Changing the JSON output schema in a backward-incompatible way.
- Changing the blueprint schema in a backward-incompatible way.
- Changing the manifest schema in a backward-incompatible way.

Breaking changes are **always** documented in the `[Unreleased]` → `### Changed`
section with a `BREAKING CHANGE:` note, and are accompanied by migration
guidance in `docs/upgrade/`.

---

## Changelog Categories

This changelog uses the following categories, inspired by
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/):

| Category | Description |
| :--- | :--- |
| **Added** | New features, commands, flags, blueprints, or documentation. |
| **Changed** | Changes to existing functionality. |
| **Deprecated** | Features that will be removed in a future release. |
| **Removed** | Features that have been removed. |
| **Fixed** | Bug fixes. |
| **Security** | Security improvements or vulnerability fixes. |
| **Known Limitations** | Documented limitations of the release. |
| **Upgrade Notes** | Migration guidance for breaking changes. |
| **Feedback** | How to report issues and provide feedback. |

---

## How to Update This File

### For Contributors

When you open a pull request that changes user-facing behavior, add an entry to
the `[Unreleased]` section under the appropriate category. Use the same
[Conventional Commits](https://www.conventionalcommits.org/) style as your
commit messages.

**Example:**

```markdown
### Added

- **CLI** — Add `--json` flag to `forge verify` for machine-readable output.
  (#123)
```

Include the PR number in parentheses at the end of the entry.

### For Maintainers

At release time, the maintainer:

1. Renames `[Unreleased]` to the new version (e.g., `[0.1.0a0]`).
2. Adds the release date (e.g., `— 2026-11-XX`).
3. Creates a new `[Unreleased]` section at the top with empty categories.
4. Updates the comparison links at the bottom of the file.
5. Commits with a message like `chore(release): prepare v0.1.0a0`.
6. Tags the commit (e.g., `git tag -a v0.1.0a0 -m "Release v0.1.0a0"`).
7. Pushes the tag, which triggers the release workflow.

### Automation

This changelog can be automated using one of the following tools:

- **[`towncrier`](https://towncrier.readthedocs.io/)** — Fragment-based
  changelog generation. Each PR adds a small `.changes/<type>/<PR>.md` file,
  and `towncrier build` assembles the changelog at release time.
- **[`git-cliff`](https://git-cliff.org/)** — Git-history-based changelog
  generation. Parses Conventional Commits and generates the changelog
  automatically.

The project brief recommends `towncrier` or a conventional-changelog approach.
Either is compatible with this file's structure.

---

## Comparison Links

At the bottom of the file, add links for comparing versions on GitHub. This is
a standard Keep a Changelog convention.

```markdown
[Unreleased]: https://github.com/your-org/forge-cli/compare/v0.1.0a0...HEAD
[0.1.0a0]: https://github.com/your-org/forge-cli/releases/tag/v0.1.0a0
```

When a new version is released, add a new comparison link. For example, if
`0.1.0` is released after `0.1.0a0`:

```markdown
[Unreleased]: https://github.com/your-org/forge-cli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/your-org/forge-cli/compare/v0.1.0a0...v0.1.0
[0.1.0a0]: https://github.com/your-org/forge-cli/releases/tag/v0.1.0a0
```

---

<div align="center">

**Thank you for using Forge CLI.**

**Stop rebuilding the foundation. Start building the product.**

</div>
