"""Tests for `forge validate`'s exit-code contract.

Scope
-----
This file is the *contract test* for `forge validate`'s exit codes.
It is deliberately narrower than `test_validate_paths.py` (which also
covers path resolution and message rendering): here we only care about
the numbers the process hands back to the shell, because those numbers
are what CI pipelines and scripts branch on.

The contract being verified is ADR-0013:

    * 0 -- SUCCESS          : the blueprint validated cleanly.
    * 1 -- USER_ERROR       : the blueprint is invalid (schema, model,
                              filesystem, or YAML-parse failure).
    * 2 -- BLUEPRINT_ERROR  : the *invocation* is wrong, not the
                              blueprint (directory without a manifest).
    * 5 -- INTERNAL_ERROR   : the loader itself crashed; a Forge bug,
                              not the user's fault.

Two failure modes are *not* covered here:

    * Usage errors emitted by Typer *before* the callback runs (missing
      PATH, path does not exist, unreadable path). Those are exercised
      at the integration tier (``test_validate_command.py``), which is
      the only tier where Typer's argument validators run.
    * ``--debug`` re-raising behaviour. It is covered in
      ``test_validate_paths.py`` where it belongs (the flag is a
      debug-mode concern, not an exit-code one; the exit code is
      identical either way -- the process either returns 5 or the
      interpreter sees the raise and produces its own status).

Why the callback is called directly, and why through ``_exit_code``
-------------------------------------------------------------------
``validate`` raises ``typer.Exit`` on every exit path -- the mechanism
Typer documents for exit codes under ``standalone_mode=False``. The
helper unwraps that raise into an ``int`` so the test bodies can
continue to assert on the exit code as a value. See
``tests/unit/cli/_validate_helpers.py`` for the full reasoning.

The four tests below correspond to the four lines of ADR-0013's table
this command can emit. Each one is named after the contract, not after
the implementation, so a future refactor that preserves the contract
does not have to update these tests -- only a *change to the contract*
does.

Rendering is not asserted here
------------------------------
``test_validate_all_errors.py`` covers rendering (message content,
whitespace-insensitivity, hint presence). This file asserts only the
integer. That separation means a rendering change can never break
these tests, and a contract change can never be hidden by a
rendering assertion. Two concerns, two files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.cli.commands.validate import validate
from forge.core.exceptions import ExitCode
from tests.unit.cli._validate_helpers import _exit_code

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
_VALID_BLUEPRINT = """\
schema_version: "1.0.0"
id: sample.valid
name: Valid Blueprint
version: 1.0.0
description: Minimal blueprint used only in exit-code tests.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""

_INVALID_BLUEPRINT = """\
schema_version: "1.0.0"
id: sample.invalid
name: Invalid Blueprint
version: "1.0"
description: Invalid blueprint used only in exit-code tests.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""


def _write(directory: Path, body: str) -> Path:
    """Write ``blueprint.yaml`` in ``directory`` and return the manifest path."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "blueprint.yaml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


# -----------------------------------------------------------------------------
# 0 — SUCCESS
# -----------------------------------------------------------------------------
class TestSuccessExitCode:
    """A valid blueprint exits 0 — the only "nothing wrong" signal."""

    def test_valid_blueprint_file_returns_success(self, tmp_path: Path) -> None:
        """A file path to a valid blueprint returns 0."""
        manifest = _write(tmp_path / "bp", _VALID_BLUEPRINT)
        assert _exit_code(lambda: validate(path=manifest, json=False)) == ExitCode.SUCCESS

    def test_valid_blueprint_directory_returns_success(self, tmp_path: Path) -> None:
        """A directory path to a valid blueprint returns 0."""
        _write(tmp_path / "bp", _VALID_BLUEPRINT)
        assert _exit_code(lambda: validate(path=tmp_path / "bp", json=False)) == ExitCode.SUCCESS


# -----------------------------------------------------------------------------
# 1 — USER_ERROR
# -----------------------------------------------------------------------------
class TestUserErrorExitCode:
    """A blueprint that fails validation exits 1."""

    def test_schema_invalid_blueprint_returns_user_error(self, tmp_path: Path) -> None:
        """A schema violation (bad SemVer) exits 1."""
        manifest = _write(tmp_path / "bp", _INVALID_BLUEPRINT)
        assert _exit_code(lambda: validate(path=manifest, json=False)) == ExitCode.USER_ERROR

    def test_yaml_parse_failure_returns_user_error(self, tmp_path: Path) -> None:
        """Malformed YAML exits 1 (not 5, not 2)."""
        manifest = tmp_path / "broken.yaml"
        manifest.write_text("id: [unclosed\n", encoding="utf-8")
        assert _exit_code(lambda: validate(path=manifest, json=False)) == ExitCode.USER_ERROR

    def test_missing_template_file_returns_user_error(self, tmp_path: Path) -> None:
        """A blueprint whose template does not exist exits 1."""
        body = """\
schema_version: "1.0.0"
id: sample.missing_template
name: Missing Template
version: 1.0.0
description: References a template that does not exist.
category: api
files:
  - source: templates/nope.j2
    destination: nope.txt
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""
        manifest = _write(tmp_path / "bp", body)
        assert _exit_code(lambda: validate(path=manifest, json=False)) == ExitCode.USER_ERROR


# -----------------------------------------------------------------------------
# 2 — BLUEPRINT_ERROR
# -----------------------------------------------------------------------------
class TestBlueprintErrorExitCode:
    """A directory without a manifest exits 2."""

    def test_directory_without_manifest_returns_blueprint_error(self, tmp_path: Path) -> None:
        """A directory with no ``blueprint.yaml`` exits 2."""
        empty = tmp_path / "not-a-blueprint"
        empty.mkdir()
        assert _exit_code(lambda: validate(path=empty, json=False)) == ExitCode.BLUEPRINT_ERROR


# -----------------------------------------------------------------------------
# 5 — INTERNAL_ERROR
# -----------------------------------------------------------------------------
class TestInternalErrorExitCode:
    """A loader crash exits 5, not 1 — because it is a Forge bug."""

    def test_loader_exception_returns_internal_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A loader crash returns 5 with ``--debug`` off (default)."""
        from forge.cli.commands import validate as validate_module

        manifest = _write(tmp_path / "bp", _VALID_BLUEPRINT)

        class _BoomLoader:
            def load(self, *_args, **_kwargs):
                raise RuntimeError("simulated loader crash")

        monkeypatch.setattr(validate_module, "BlueprintLoader", _BoomLoader)
        monkeypatch.setattr(validate_module, "_debug_requested", lambda: False)

        assert _exit_code(lambda: validate(path=manifest, json=False)) == ExitCode.INTERNAL_ERROR


# -----------------------------------------------------------------------------
# Sanity: the exit-code enum is what we think it is
# -----------------------------------------------------------------------------
class TestExitCodeEnum:
    """Pin the four values this command emits."""

    def test_contract_values(self) -> None:
        assert ExitCode.SUCCESS == 0
        assert ExitCode.USER_ERROR == 1
        assert ExitCode.BLUEPRINT_ERROR == 2
        assert ExitCode.INTERNAL_ERROR == 5
