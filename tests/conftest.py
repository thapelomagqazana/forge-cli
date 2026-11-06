"""Shared pytest configuration and fixtures for the Forge CLI test suite.

This module is intentionally the *only* place where global pytest behaviour
is defined. Every fixture here is used by more than one test module; test-
specific fixtures belong in the test file that needs them.

Design principles
-----------------
1. **Isolation**: every fixture that touches the filesystem uses ``tmp_path``.
   No test may read or write the developer's real home directory, the real
   ``.forge/`` metadata, or the real environment.
2. **Typed**: every fixture declares its return type so mypy strict can
   verify test code as carefully as production code.
3. **Explicit markers**: markers are registered here so ``--strict-markers``
   catches typos in ``@pytest.mark.*`` decorators.
4. **No global state leakage**: fixtures that mutate ``os.environ`` or the
   current working directory always restore the previous state on teardown.
5. **Layered**: fixtures are grouped by layer (environment, settings,
   blueprints, CLI, filesystem, subprocess) so a reader can find what they
   need without scanning the whole file.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from forge.core.config import Settings


# =============================================================================
# Marker registration
# =============================================================================
def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers to satisfy ``--strict-markers``.

    Without this, any ``@pytest.mark.unit`` (etc.) decorator would fail with
    ``--strict-markers`` enabled. Keeping the list here means the canonical
    set of markers is defined in exactly one place.

    The marker list must stay in sync with ``[tool.pytest.ini_options]``
    in ``pyproject.toml``.
    """
    markers = [
        "unit: Fast, isolated unit tests",
        "integration: Integration tests (filesystem, CLI, subprocess)",
        "contract: Contract tests (JSON schemas, CLI output envelopes)",
        "e2e: End-to-end CLI tests (invoke the installed ``forge`` binary)",
        "security: Security-focused tests (path traversal, injection, hooks)",
        "slow: Slow tests (deselect with ``-m 'not slow'``)",
    ]
    for marker in markers:
        config.addinivalue_line("markers", marker)


# =============================================================================
# Environment and settings isolation
# =============================================================================
@pytest.fixture(autouse=True)
def _isolate_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Isolate every test from the developer's real environment.

    This fixture is ``autouse=True`` — it runs for *every* test without
    requiring an explicit request. It does three things:

    1. Clears all ``FORGE_*`` environment variables so settings from the
       developer's shell cannot leak into tests.
    2. Points ``HOME`` / ``USERPROFILE`` at a temp directory so tests never
       read or write the real user's home directory.
    3. Sets ``XDG_CONFIG_HOME`` and ``XDG_CACHE_HOME`` at a temp directory
       so any config or cache written by ``forge`` goes into ``tmp_path``.

    Removing this fixture is a bug: tests would start touching the real
    developer home directory, and would produce different results on
    different machines.
    """
    for key in list(os.environ):
        if key.startswith("FORGE_"):
            monkeypatch.delenv(key, raising=False)

    fake_home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(fake_home / ".cache"))


@pytest.fixture()
def isolated_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Path]:
    """Run the test in an isolated working directory.

    Yields the temporary directory and restores the original cwd on
    teardown, even if the test raises.
    """
    original = Path.cwd()
    monkeypatch.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        monkeypatch.chdir(original)


# =============================================================================
# Settings fixture
# =============================================================================
@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Return a fully-isolated ``Settings`` instance for the current test.

    All paths point inside ``tmp_path`` so nothing touches the real
    filesystem.
    """
    return Settings(
        log_level="DEBUG",
        blueprint_dir=str(tmp_path / "blueprints"),
    )


# =============================================================================
# CLI fixtures
# =============================================================================
# These fixtures are the canonical way to invoke the Forge CLI in tests.
# They bind to the app built by ``create_app()``, not the module-level
# ``main`` function, so tests that register ``_probe_*`` commands get a
# fresh app instance and do not interfere with each other.
#
# If a test needs to exercise ``main()`` itself (the entry point that
# installs the exception handler), it should use ``subprocess`` with
# ``python -m forge.cli`` — see ``test_python_m_entry_point`` in
# ``tests/unit/cli/test_main.py`` for the reference implementation.
# =============================================================================
@pytest.fixture()
def cli_runner() -> CliRunner:
    """Return a ``CliRunner`` bound to the Forge CLI app.

    Click 8.2+ removed the ``mix_stderr`` parameter because stdout and
    stderr are now always captured separately. Typer 0.28 inherits that
    behaviour. The parameter is omitted.

    Usage::

        def test_something(cli_runner, forge_app):
            result = cli_runner.invoke(forge_app, ["ping"])
            assert result.exit_code == 0
    """
    return CliRunner()


@pytest.fixture()
def forge_app() -> typer.Typer:
    """Return a fresh Forge ``Typer`` application.

    A *fresh* app per test is important: tests that register ``_probe_*``
    commands (see ``tests/unit/cli/test_main.py``) must not leak those
    commands into other tests.

    The fixture is a factory call, not a module-level import, because the
    module-level app would be shared across tests.
    """
    from forge.cli.main import create_app

    return create_app()


@pytest.fixture()
def forge_executable() -> Iterator[str]:
    """Yield the command prefix used to invoke Forge as a subprocess.

    Returns a list (as a single string) that tests can split and pass to
    ``subprocess.run``. Prefers the real ``forge`` console script when it
    is on ``PATH``; falls back to ``python -m forge.cli`` so the fixture
    works in every environment, including CI containers that do not
    activate the virtualenv.

    Note: the fallback module is ``forge.cli``, not ``forge``. The
    ``forge`` module is the package; the ``forge.cli`` module is the
    entry point with a ``__main__`` block.
    """
    from shutil import which

    exe = which("forge")
    if exe is not None:
        yield exe
        return

    # Fallback: invoke the CLI module directly. Works on every OS and every
    # environment, even when the console script is not on PATH.
    yield f"{sys.executable} -m forge.cli"


# =============================================================================
# Blueprint fixtures
# =============================================================================
@pytest.fixture()
def sample_blueprint_dict() -> dict[str, Any]:
    """Minimal valid blueprint payload for schema and loader tests."""
    return {
        "schema_version": "1.0.0",
        "blueprint": {
            "id": "python.fastapi.api",
            "name": "Python FastAPI API",
            "version": "1.0.0",
            "description": "Production-oriented FastAPI project",
            "category": "backend",
        },
        "variables": {
            "project_name": {
                "type": "string",
                "description": "The project name (lowercase, hyphenated).",
                "required": True,
                "validation": {"pattern": "^[a-z][a-z0-9-]*$"},
            },
            "include_docker": {
                "type": "boolean",
                "description": "Include a Dockerfile and docker-compose.yml.",
                "default": True,
            },
        },
        "files": [
            {
                "source": "src/{{ project_name }}/main.py.j2",
                "destination": "src/{{ project_name }}/main.py",
            },
        ],
        "verification": {
            "checks": [
                {
                    "id": "required-files",
                    "type": "required_file",
                    "path": "pyproject.toml",
                },
                {
                    "id": "tests",
                    "type": "command",
                    "command": "pytest",
                    "timeout": 120,
                },
            ],
        },
    }


@pytest.fixture()
def sample_blueprint_path(
    tmp_path: Path,
    sample_blueprint_dict: dict[str, Any],
) -> Path:
    """Write ``sample_blueprint_dict`` to disk as ``blueprint.yaml``.

    Returns the path to the written file. Useful for loader and validator
    tests that need a real file on disk.
    """
    import yaml  # local import: avoid a hard dependency at collection time

    blueprint_dir = tmp_path / "blueprint"
    blueprint_dir.mkdir()
    blueprint_file = blueprint_dir / "blueprint.yaml"
    blueprint_file.write_text(
        yaml.safe_dump(sample_blueprint_dict, sort_keys=False),
        encoding="utf-8",
    )
    return blueprint_file


# =============================================================================
# Filesystem fixtures
# =============================================================================
@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Return an empty directory that represents a generated project root."""
    project = tmp_path / "my-project"
    project.mkdir()
    return project


@pytest.fixture()
def fake_forge_manifest(project_root: Path) -> Path:
    """Write a minimal, valid ``.forge/manifest.json`` into ``project_root``.

    Returns the path to the manifest file. Used by ``forge verify``,
    ``forge inspect``, and drift-detection tests.
    """
    import json

    forge_dir = project_root / ".forge"
    forge_dir.mkdir()
    manifest = {
        "manifest_schema_version": "1.0.0",
        "project": {
            "id": "0190f3c8-7c12-7b41-8d55-8e8a0f7d2c10",
            "name": "my-project",
            "created_at": "2026-01-01T00:00:00Z",
        },
        "forge": {"version": "0.1.0a0"},
        "blueprint": {
            "id": "python.fastapi.api",
            "version": "1.0.0",
            "source": "forge-official",
        },
        "configuration": {"selected_options": {"include_docker": True}},
        "verification": {
            "expected_baseline_checks": ["required-files", "tests"],
        },
    }
    manifest_path = forge_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest_path
