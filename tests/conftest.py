"""Shared pytest configuration and fixtures for the Forge CLI test suite.

This module is intentionally the *only* place where global pytest behaviour
is defined. Every fixture here is used by more than one test module; test-
specific fixtures belong in the test file that needs them.

Design principles
-----------------
1. **Isolation**: every fixture that touches the filesystem uses ``tmp_path``.
   No test may read or write the developer's real home directory, the real
   ``.forge/`` metadata, or the real environment.
2. **Typed**: every fixture declares its return type so mypy strict can verify
   test code as carefully as production code.
3. **Explicit markers**: markers are registered here so ``--strict-markers``
   catches typos in ``@pytest.mark.*`` decorators.
4. **No global state leakage**: fixtures that mutate ``os.environ`` or the
   current working directory always restore the previous state on teardown.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from forge.core.config import Settings


# -----------------------------------------------------------------------------
# Marker registration
# -----------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers to satisfy ``--strict-markers``.

    Without this, any ``@pytest.mark.unit`` (etc.) decorator would fail with
    ``--strict-markers`` enabled. Keeping the list here means the canonical
    set of markers is defined in exactly one place.
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


# -----------------------------------------------------------------------------
# Environment and settings isolation
# -----------------------------------------------------------------------------
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
) -> Path:
    """Run the test in an isolated working directory.

    Yields the temporary directory and restores the original cwd on teardown,
    even if the test raises.
    """
    original = Path.cwd()
    monkeypatch.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        monkeypatch.chdir(original)


# -----------------------------------------------------------------------------
# Settings fixture
# -----------------------------------------------------------------------------
@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Return a fully-isolated ``Settings`` instance for the current test.

    All paths point inside ``tmp_path`` so nothing touches the real filesystem.
    """
    return Settings(
        log_level="DEBUG",
        blueprint_dir=str(tmp_path / "blueprints"),
    )


# -----------------------------------------------------------------------------
# Blueprint fixtures
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# CLI fixtures
# -----------------------------------------------------------------------------
@pytest.fixture()
def cli_runner() -> Any:
    """Return a Typer ``CliRunner`` bound to the Forge CLI app.

    The returned object exposes ``invoke(app, args=...)``. Tests should
    assert on ``result.exit_code``, ``result.stdout``, and ``result.stderr``.
    """
    from typer.testing import CliRunner

    return CliRunner(mix_stderr=False)


@pytest.fixture()
def forge_app() -> Any:
    """Return the root Typer application object.

    Kept as a fixture (rather than a module-level import) so that importing
    the CLI does not fail if a command module has a transient import error
    during a large refactor. Failures surface as test failures, not as
    collection errors.
    """
    from forge.cli.app import app

    return app


# -----------------------------------------------------------------------------
# Filesystem fixtures
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Subprocess fixtures (end-to-end tests)
# -----------------------------------------------------------------------------
@pytest.fixture()
def forge_executable() -> Iterator[str]:
    """Yield the path to the installed ``forge`` executable.

    Uses ``sys.executable -m forge`` as a portable fallback so the fixture
    works even if the ``forge`` console script is not on PATH (e.g., when
    tests run under ``uv run pytest`` without activating the venv).
    """
    # Prefer the real console script when it exists.
    from shutil import which

    exe = which("forge")
    if exe is not None:
        yield exe
        return

    # Fall back to ``python -m forge``.
    yield f"{sys.executable} -m forge"
