# Contributing to Forge CLI

Thank you for your interest in contributing to Forge CLI! This document explains
how to set up your development environment, the standards we follow, and the
process for getting your change merged.

We welcome contributions of all kinds:

- 🐛 **Bug reports** — found something broken? [Open an issue](https://github.com/your-org/forge-cli/issues/new?template=bug_report.yml).
- ✨ **Feature requests** — have an idea? [Open an issue](https://github.com/your-org/forge-cli/issues/new?template=blueprint_request.yml).
- 📖 **Documentation** — typos, unclear explanations, missing examples.
- 🧪 **Tests** — new test cases, better coverage, edge cases.
- 🔧 **Code** — bug fixes, new features, refactors.
- 🎨 **Blueprints** — new project foundations (see [Blueprint Contribution](#blueprint-contribution)).

By participating in this project, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

---

## Table of Contents

- [Quick Start for Contributors](#quick-start-for-contributors)
- [Development Environment](#development-environment)
- [Project Structure](#project-structure)
- [Commit Convention](#commit-convention)
- [Code Style](#code-style)
- [Testing Requirements](#testing-requirements)
- [Pull Request Process](#pull-request-process)
- [Definition of Done](#definition-of-done)
- [Blueprint Contribution](#blueprint-contribution)
- [Documentation Contribution](#documentation-contribution)
- [Reporting Security Issues](#reporting-security-issues)
- [Community and Support](#community-and-support)
- [License](#license)

---

## Quick Start for Contributors

```bash
# 1. Fork the repository on GitHub, then clone your fork
git clone https://github.com/<your-username>/forge-cli.git
cd forge-cli

# 2. Install uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install all dependencies (including dev tools)
uv sync --all-extras

# 4. Install pre-commit hooks
uv run pre-commit install

# 5. Verify everything works
uv run pytest
uv run ruff check .
uv run mypy src

# 6. Create a feature branch
git checkout -b feat/your-feature-name
```

You are now ready to make changes. See [Development Environment](#development-environment)
for more details on the toolchain.

---

## Development Environment

### Prerequisites

| Tool | Version | Purpose |
| :--- | :--- | :--- |
| **Python** | 3.12+ | The language Forge CLI is written in. |
| **uv** | Latest | Fast Python package and project manager. |
| **Git** | 2.40+ | Version control. |
| **Docker** | Optional | Required for the `docker_build` verification check. |

### Installation

Forge CLI uses **`uv`** for dependency management and lockfiles. Do **not** use
`pip install -e .` directly — it bypasses the lockfile and can produce
non-reproducible environments.

```bash
# Install uv (Linux/macOS)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install uv (Windows PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Sync all dependencies (including dev tools)
uv sync --all-extras
```

### Common Commands

| Task | Command |
| :--- | :--- |
| Run the CLI locally | `uv run forge --help` |
| Run all tests | `uv run pytest` |
| Run tests with coverage | `uv run pytest --cov=src/forge --cov-report=term-missing` |
| Run a single test file | `uv run pytest tests/unit/test_config.py -v` |
| Run tests by marker | `uv run pytest -m unit` |
| Lint the code | `uv run ruff check .` |
| Auto-fix lint issues | `uv run ruff check . --fix` |
| Format the code | `uv run ruff format .` |
| Type-check the code | `uv run mypy src` |
| Run security scan | `uv run bandit -r src/ -ll` |
| Audit dependencies | `uv run pip-audit` |
| Run pre-commit hooks manually | `uv run pre-commit run --all-files` |
| Build the package | `uv build` |
| Install the package locally | `uv tool install --editable .` |

### Editor Setup

We recommend **Visual Studio Code** with the following extensions:

- [Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
- [Ruff](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff)
- [Mypy Type Checker](https://marketplace.visualstudio.com/items?itemName=ms-python.mypy-type-checker)
- [Even Better TOML](https://marketplace.visualstudio.com/items?itemName=tamasfe.even-better-toml)

Add this to your `.vscode/settings.json` (gitignored, per-user):

```json
{
  "python.defaultInterpreterPath": ".venv/bin/python",
  "editor.formatOnSave": true,
  "editor.codeActionsOnSave": {
    "source.fixAll.ruff": "explicit",
    "source.organizeImports.ruff": "explicit"
  },
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff"
  },
  "mypy-type-checker.args": ["--config-file=pyproject.toml"]
}
```

---

## Project Structure

```
forge-cli/
├── src/forge/                  # The Forge CLI package
│   ├── cli/                    # CLI commands and entry point
│   │   ├── app.py              # Typer application factory
│   │   └── commands/           # One module per command
│   ├── core/                   # Shared domain logic
│   │   ├── config.py           # Configuration management
│   │   ├── exceptions.py       # Domain exceptions and exit codes
│   │   └── models.py           # Shared Pydantic models
│   ├── blueprints/             # Official blueprint definitions
│   │   ├── python-api/
│   │   ├── typescript-frontend/
│   │   └── python-automation/
│   ├── generation/             # Template rendering and project generation
│   ├── verification/           # Verification engine and check handlers
│   ├── manifests/              # Project manifest generation and reading
│   ├── diagnostics/            # `forge doctor` implementation
│   ├── output/                 # JSON and human-readable output
│   └── schemas/                # JSON Schemas (blueprint, manifest, output)
├── tests/                      # Test suite
│   ├── unit/                   # Fast, isolated tests
│   ├── integration/            # Tests that touch the filesystem
│   ├── contract/               # Schema and output contract tests
│   ├── e2e/                    # End-to-end CLI tests
│   ├── security/               # Path traversal, injection, rollback tests
│   └── fixtures/               # Shared test fixtures
├── docs/                       # Documentation (MkDocs Material)
├── examples/                   # Example projects generated by Forge
├── demos/                      # VHS scripts for terminal demos
├── pyproject.toml              # Project metadata and tool configuration
├── uv.lock                     # Lockfile (do not edit manually)
├── CONTRIBUTING.md             # This file
├── SECURITY.md                 # Security policy
├── CODE_OF_CONDUCT.md          # Community standards
├── CHANGELOG.md                # Release history
└── LICENSE                     # Apache 2.0
```

**Rule of thumb:** if a change touches `src/forge/cli/commands/`, it almost
always needs a corresponding change in `tests/e2e/` or `tests/contract/`.

---

## Commit Convention

We follow [**Conventional Commits**](https://www.conventionalcommits.org/en/v1.0.0/).
This enables automatic changelog generation and semantic versioning.

### Format

```
<type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

### Types

| Type | When to Use |
| :--- | :--- |
| `feat` | A new feature (triggers a MINOR version bump). |
| `fix` | A bug fix (triggers a PATCH version bump). |
| `docs` | Documentation changes only. |
| `test` | Adding or fixing tests. |
| `refactor` | A code change that neither fixes a bug nor adds a feature. |
| `perf` | A performance improvement. |
| `build` | Changes to the build system or dependencies. |
| `ci` | Changes to CI configuration. |
| `chore` | Other changes that don't modify `src/` or `tests/`. |
| `revert` | Reverting a previous commit. |

### Scopes

Use the module name as the scope. Common scopes:

- `cli` — CLI commands and entry point.
- `core` — Core domain logic.
- `blueprints` — Blueprint definitions.
- `generation` — Template rendering and project generation.
- `verification` — Verification engine.
- `manifests` — Manifest handling.
- `diagnostics` — `forge doctor`.
- `output` — JSON and human-readable output.
- `docs` — Documentation.
- `deps` — Dependency updates.

### Examples

```
feat(cli): add --json flag to forge verify
fix(generation): prevent path traversal in template destination
docs(blueprints): document the variable schema
test(verification): add timeout handling test for command runner
refactor(core): extract exit code mapping into a dedicated module
```

### Breaking Changes

Append `!` after the type/scope, and include a `BREAKING CHANGE:` footer:

```
feat(cli)!: rename `forge new` to `forge create`

BREAKING CHANGE: The `forge new` command has been renamed to `forge create`
for consistency with the rest of the CLI. Update any scripts that call
`forge new` directly.
```

---

## Code Style

### Python

- **Python 3.12+** features are encouraged (`match`, `|` union types, `typing.override`, etc.).
- **Line length:** 100 characters (configured in `pyproject.toml`).
- **Formatting:** enforced by **Ruff** (`ruff format`).
- **Linting:** enforced by **Ruff** with an extensive rule set
  (`E`, `F`, `I`, `N`, `W`, `UP`, `B`, `C4`, `SIM`, `RUF`).
- **Type hints:** required on all public functions and methods.
  Enforced by **Mypy** in `strict` mode.
- **Docstrings:** required on all public modules, classes, and functions.
  Follow [PEP 257](https://peps.python.org/pep-0257/). We use the
  **Google style** for multi-line docstrings.
- **Imports:** organized by Ruff (`I` rule set). Standard library first,
  then third-party, then first-party (`forge`).

### Example

```python
"""Module for resolving variable values from multiple sources."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge.core.exceptions import UserInputError


@dataclass(frozen=True)
class ResolvedVariable:
    """A variable that has been resolved and validated."""

    name: str
    value: str | bool
    source: str  # "cli", "prompt", or "default"


def resolve_variable(
    name: str,
    cli_value: str | None,
    default: str | bool | None,
    required: bool,
) -> ResolvedVariable:
    """Resolve a variable from CLI input, default, or fail if required.

    Args:
        name: The variable's stable identifier.
        cli_value: The value passed on the command line, if any.
        default: The blueprint-defined default, if any.
        required: Whether the variable must have a value.

    Returns:
        A ``ResolvedVariable`` with the value and its source.

    Raises:
        UserInputError: If the variable is required and no value is available.
    """
    if cli_value is not None:
        return ResolvedVariable(name=name, value=cli_value, source="cli")
    if default is not None:
        return ResolvedVariable(name=name, value=default, source="default")
    if required:
        raise UserInputError(f"Required variable '{name}' was not provided.")
    return ResolvedVariable(name=name, value="", source="default")
```

### YAML and JSON

- **Indentation:** 2 spaces (enforced by `.editorconfig`).
- **Quotes:** prefer double quotes in YAML for consistency with JSON.
- **Ordering:** keys in schemas should follow a logical order
  (metadata → variables → files → verification).

### Markdown

- **Line length:** not enforced, but prefer wrapping at ~100 characters.
- **Code blocks:** always specify a language (`bash`, `python`, `yaml`, `json`, `text`).
- **Links:** use relative links for internal docs, absolute for external.

### Commit messages

- **Subject line:** imperative mood, lowercase, no trailing period, ≤ 72 characters.
- **Body:** wrap at 72 characters, explain the *why* (not the *what* — the diff
  already shows the what).

---

## Testing Requirements

Forge CLI is an **engineering tool**. Its own quality must demonstrate the
standards it promotes. Testing is a first-class product concern.

### Test Pyramid

| Level | Location | Purpose | Speed |
| :--- | :--- | :--- | :--- |
| **Unit** | `tests/unit/` | Test pure logic in isolation. | Fast (< 1s each) |
| **Integration** | `tests/integration/` | Test components that touch the filesystem or subprocesses. | Medium (< 5s each) |
| **Contract** | `tests/contract/` | Test schema compatibility and output contracts. | Fast |
| **E2E** | `tests/e2e/` | Test the full CLI via `subprocess`. | Slow (< 60s each) |
| **Security** | `tests/security/` | Test path traversal, injection, rollback, symlinks. | Medium |

### Coverage Requirements

- **Minimum coverage:** 90% (enforced in CI via `--cov-fail-under=90`).
- **Critical modules** (`core/`, `generation/`, `verification/`): 95%.
- **Security-critical modules** (`generation/safety/`): 100%.

### Test Markers

Use the markers registered in `pyproject.toml`:

```python
import pytest


@pytest.mark.unit
def test_resolve_variable_from_cli() -> None:
    ...


@pytest.mark.integration
def test_generation_creates_project(tmp_path: Path) -> None:
    ...


@pytest.mark.security
def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    ...


@pytest.mark.slow
def test_full_blueprint_verification() -> None:
    ...
```

### Running Tests

```bash
# All tests (fast)
uv run pytest

# Only unit tests
uv run pytest -m unit

# Only integration tests
uv run pytest -m integration

# All except slow tests (recommended for local development)
uv run pytest -m "not slow"

# With coverage report
uv run pytest --cov=src/forge --cov-report=html
open htmlcov/index.html  # view the report
```

### Test Fixtures

Shared fixtures live in `tests/conftest.py`. Blueprint fixtures live in
`tests/fixtures/`. When adding a new fixture:

1. Place it in the appropriate subdirectory.
2. Document it in `tests/fixtures/README.md`.
3. Reference it via a `@pytest.fixture` in `conftest.py` if it is broadly useful.

### What to Test

Every pull request **must** include tests for:

- **New behavior:** at least one positive and one negative test.
- **Bug fixes:** a regression test that fails before the fix and passes after.
- **Security-sensitive code:** adversarial tests (e.g., path traversal, injection).
- **Public API changes:** contract tests validating the new schema.

If a change cannot be tested (e.g., a documentation update), say so explicitly
in the pull request description.

---

## Pull Request Process

### Before You Open a PR

1. **Search existing issues** to avoid duplicating work.
2. **Open an issue first** for non-trivial changes (features, refactors) so
   maintainers can confirm the direction before you invest time.
3. **Run the full local check suite:**

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   uv run pytest --cov=src/forge --cov-fail-under=90
   uv run bandit -r src/ -ll
   ```

4. **Update the changelog** in `CHANGELOG.md` under the `[Unreleased]` section.
5. **Update documentation** if the change is user-facing (commands, config,
   blueprint schema, output).

### Opening the PR

Use the [pull request template](.github/PULL_REQUEST_TEMPLATE.md). Include:

- **Summary:** what changed and why.
- **Related issues:** `Closes #123`.
- **Type of change:** bug fix / feature / breaking change / docs / refactor.
- **Checklist:** confirm all items are complete.
- **Screenshots or terminal output** for user-facing changes.

### Review Process

- A maintainer will review your PR within **5 business days**.
- Address review comments by pushing new commits (do not force-push during review).
- Once approved, a maintainer will squash-merge the PR into `main`.
- The commit message will be derived from your PR title (which must follow
  [Conventional Commits](#commit-convention)).

### What Maintainers Look For

| Criterion | Why It Matters |
| :--- | :--- |
| **Correctness** | Does the change do what it claims? Is it tested? |
| **Safety** | Does it introduce path traversal, injection, or overwrite risks? |
| **Backward compatibility** | Does it break existing projects, commands, or schemas? |
| **Performance** | Does it introduce a regression in generation or verification time? |
| **Documentation** | Is the change documented for users and future contributors? |
| **Scope** | Is the PR focused on one concern? Large PRs are harder to review. |

### PR Size Guidelines

- **Small PRs (< 200 lines):** reviewed within 2 days.
- **Medium PRs (200–500 lines):** reviewed within 5 days.
- **Large PRs (> 500 lines):** split into smaller PRs where possible. If not
  possible, expect a longer review and provide extra context.

---

## Definition of Done

A change is **not complete** until **all** of the following are true:

### Code

- [ ] The implementation is merged into `main` via a reviewed PR.
- [ ] All new code has type hints and docstrings.
- [ ] The code passes `ruff check` and `ruff format --check`.
- [ ] The code passes `mypy --strict`.

### Tests

- [ ] New behavior is covered by unit tests (positive and negative cases).
- [ ] Integration tests cover filesystem or subprocess behavior.
- [ ] Security tests cover adversarial inputs (if applicable).
- [ ] Coverage remains ≥ 90% overall and ≥ 95% for critical modules.
- [ ] All tests pass on Linux, Windows, and macOS in CI.

### Documentation

- [ ] User-facing changes are documented in `docs/`.
- [ ] Command reference is updated (if a command or flag changed).
- [ ] Blueprint reference is updated (if the schema changed).
- [ ] `CHANGELOG.md` is updated under `[Unreleased]`.
- [ ] Code examples in documentation are tested.

### Compatibility

- [ ] The change is backward-compatible, **or** a migration is documented.
- [ ] JSON output schemas are versioned if they changed.
- [ ] Blueprint schema versions are bumped if they changed.

### Release

- [ ] The change is included in the next release notes.
- [ ] The change can be deployed via the documented release process.

---

## Blueprint Contribution

Blueprints are the heart of Forge CLI. A well-designed blueprint saves teams
hours of setup; a poorly designed one reproduces the very problems Forge exists
to solve.

### Blueprint Quality Standard

Every official blueprint must:

1. **Pass schema validation** against `blueprint.schema.json`.
2. **Render without errors** into a clean temporary directory.
3. **Install dependencies successfully** in its supported environment.
4. **Pass its own tests** (the generated project's test suite).
5. **Pass linting and static checks.**
6. **Build successfully** (e.g., `vite build`, `python -m build`).
7. **Execute its documented startup workflow** (e.g., `uvicorn main:app`).
8. **Include complete setup documentation** in its README.
9. **Be tested against supported runtime versions.**
10. **Declare a maintenance owner** in its `blueprint.yaml`.

### Submitting a Blueprint

1. **Open an issue** using the [Blueprint Request template](https://github.com/your-org/forge-cli/issues/new?template=blueprint_request.yml)
   describing the use case, target users, and desired features.
2. A maintainer will confirm whether the blueprint is in scope and assign a
   reviewer.
3. **Create the blueprint** in `src/forge/blueprints/<your-blueprint-id>/`.
4. **Add a `blueprint.yaml`** with metadata, variables, files, and verification
   checks.
5. **Add a `README.md`** following the [blueprint README template](docs/blueprints/README-template.md).
6. **Add a `CHANGELOG.md`** with an initial `1.0.0` entry.
7. **Add tests** in `tests/integration/test_<your-blueprint-id>.py`.
8. **Run the blueprint validation and verification:**

   ```bash
   uv run forge validate ./src/forge/blueprints/<your-blueprint-id>
   uv run forge create <your-blueprint-id> --name test-project --no-interactive
   cd test-project
   uv run forge verify
   ```

9. **Open a PR** with the blueprint, tests, and documentation.

### Blueprint Maturity Levels

| Level | Meaning | Support |
| :--- | :--- | :--- |
| `experimental` | Early development. Not recommended for production. | Best-effort. |
| `beta` | Usable but undergoing validation. API may change. | Best-effort. |
| `stable` | Verified against published support criteria. | Full support. |
| `deprecated` | Existing users supported, but new adoption discouraged. | Security fixes only. |
| `retired` | No longer supported. | None. |

To promote a blueprint from `experimental` → `beta` → `stable`, open a PR that
updates the `maturity` field in `blueprint.yaml` and includes evidence that
the promotion criteria are met.

---

## Documentation Contribution

Documentation is as important as code. Clear documentation is what makes a tool
usable.

### Types of Documentation

| Type | Location | Purpose |
| :--- | :--- | :--- |
| **README** | `README.md` | First impression. Quick start. |
| **User guide** | `docs/getting-started/`, `docs/guides/` | Step-by-step instructions. |
| **Command reference** | `docs/commands/` | Every command and flag. |
| **Blueprint reference** | `docs/blueprints/` | Schema, variables, templates. |
| **Security** | `docs/security/` | Safety model, threat model, limitations. |
| **Architecture** | `docs/architecture/` | Internal design (for contributors). |
| **Changelog** | `CHANGELOG.md` | Release history. |

### Writing Style

- **Be concise.** Cut every word that doesn't add meaning.
- **Be precise.** Avoid vague terms like "it" or "this". Name the subject.
- **Be honest.** If something is a limitation, say so. Do not oversell.
- **Be tested.** Every command in documentation must work as documented.
- **Use code blocks.** Every command, path, and value goes in a code block
  with the correct language tag.

### Testing Documentation

All commands in documentation are tested via `pytest-codeblocks` in CI. If you
add a new command, add it to the appropriate `docs/` file and it will be tested
automatically.

For commands that require network access or Docker, use the `no-test` annotation:

````markdown
```bash no-test
docker run --rm forge-cli forge --version
```
````

---

## Reporting Security Issues

**Do not open a public issue for security vulnerabilities.**

Instead, follow the process in [SECURITY.md](SECURITY.md):

1. Email **security@forge-cli.dev** with:
   - A description of the vulnerability.
   - Steps to reproduce.
   - Potential impact.
   - Any suggested mitigation.
2. We will acknowledge receipt within **48 hours**.
3. We will provide a remediation timeline within **5 business days**.
4. We will coordinate disclosure with you.

We take security seriously. Responsible disclosure is appreciated and will be
credited in the release notes (unless you prefer to remain anonymous).

---

## Community and Support

- 💬 **GitHub Discussions:** [Ask questions, share ideas](https://github.com/your-org/forge-cli/discussions).
- 🐛 **GitHub Issues:** [Report bugs, request features](https://github.com/your-org/forge-cli/issues).
- 🐦 **Twitter/X:** [@forgecli](https://twitter.com/forgecli) for announcements.
- 📧 **Email:** [maintainers@forge-cli.dev](mailto:maintainers@forge-cli.dev) for private inquiries.

### Response Expectations

Forge CLI is maintained by a small team. We aim to:

- Acknowledge new issues within **3 business days**.
- Review pull requests within **5 business days**.
- Release security fixes within **7 days** of confirmation.

These are best-effort targets, not contractual SLAs. Community support is
provided on a volunteer basis.

---

## License

By contributing to Forge CLI, you agree that your contributions will be
licensed under the [**Apache License 2.0**](LICENSE), the same license as the
project.

You retain copyright of your contributions. By submitting a pull request, you
grant the project a perpetual, worldwide, non-exclusive, royalty-free license
to use, reproduce, modify, and distribute your contribution under the Apache
License 2.0.

---

<div align="center">

**Thank you for contributing to Forge CLI.**

Every bug report, every line of documentation, every test — it all makes
the project better.

[Back to top](#contributing-to-forge-cli)

</div>
