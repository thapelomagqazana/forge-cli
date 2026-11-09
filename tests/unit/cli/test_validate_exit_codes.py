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
      PATH, path does not exist, unreadable path). Typer's own argument
      validators raise them; Typer's own test suite exercises them.
      Re-testing Typer would be noise, and a Typer upgrade that changes
      its behaviour is a fact about Typer, not about us.
    * ``--debug`` re-raising behaviour. It is covered in
      ``test_validate_paths.py`` where it belongs (the flag is a
      debug-mode concern, not an exit-code one; the exit code is
      identical either way -- the process either returns 5 or the
      interpreter sees the raise and produces its own status).

Why the callback is called directly, not via `CliRunner`
--------------------------------------------------------
`CliRunner` is the right tool when the *test subject* is Typer's
dispatch: argument parsing, help text, subcommand routing. It is the
wrong tool here, because the subject is our exit-code mapping. Calling
the callback directly:

    * exercises exactly the code path the CLI runs after Typer's
      argument validation,
    * is an order of magnitude faster (no Click context construction,
      no environment setup),
    * makes the failure message point at our logic, not at Click's.

The four tests below correspond to the four lines of ADR-0013's table
this command can emit. Each one is named after the contract, not after
the implementation, so a future refactor that preserves the contract
does not have to update these tests — only a *change to the contract*
does.

Why every exit code is asserted against `ExitCode`, not an int
--------------------------------------------------------------
``ExitCode`` is an ``IntEnum``. ``ExitCode.USER_ERROR == 1`` and
``ExitCode.USER_ERROR is not 1`` — the first is a value comparison,
the second is an identity check. Asserting against the enum member
(rather than the integer literal) makes the test read as the contract
line it verifies, and it fails loudly if a future change to
``ExitCode`` renumbers a code (which would be a breaking change
despite any test that "still passed" because it compared to the old
literal).

Rendering is not asserted here
------------------------------
``test_validate_paths.py`` covers rendering (message content,
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

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
# Two minimal blueprints, one valid and one invalid, are enough for all
# four exit-code cases. Building them in `tmp_path` (rather than a
# shared `tests/fixtures/` directory) keeps each test independent and
# makes the failure message self-contained.

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

# A schema violation: `version` is `1.0`, not SemVer. The loader rejects
# this at the schema phase, before any model or filesystem check.
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
        """A file path to a valid blueprint returns 0.

        Passing the manifest directly is the form used by editors and
        CI jobs that know exactly which file to validate.
        """
        manifest = _write(tmp_path / "bp", _VALID_BLUEPRINT)
        assert validate(path=manifest) == ExitCode.SUCCESS

    def test_valid_blueprint_directory_returns_success(self, tmp_path: Path) -> None:
        """A directory path to a valid blueprint returns 0.

        Passing the directory is the form used by blueprint authors
        who run ``forge validate ./my-blueprint/``. Same code, same
        exit, both forms.
        """
        _write(tmp_path / "bp", _VALID_BLUEPRINT)
        assert validate(path=tmp_path / "bp") == ExitCode.SUCCESS


# -----------------------------------------------------------------------------
# 1 — USER_ERROR
# -----------------------------------------------------------------------------
class TestUserErrorExitCode:
    """A blueprint that fails validation exits 1.

    Exit 1 is the *expected* failure mode of this command: the tool
    worked, the blueprint did not. It is distinguished from exit 5
    (the tool did not work) precisely so CI can branch on "print the
    blueprint errors" vs. "open a bug against Forge".
    """

    def test_schema_invalid_blueprint_returns_user_error(self, tmp_path: Path) -> None:
        """A schema violation (bad SemVer) exits 1.

        This is the canonical "blueprint is invalid" case: the loader
        parses the YAML, runs schema validation, finds a violation,
        and returns ``Err([...])``. The command maps that to exit 1.
        """
        manifest = _write(tmp_path / "bp", _INVALID_BLUEPRINT)
        assert validate(path=manifest) == ExitCode.USER_ERROR

    def test_yaml_parse_failure_returns_user_error(self, tmp_path: Path) -> None:
        """Malformed YAML exits 1 (not 5, not 2).

        A YAML syntax error is a blueprint error — the file is found
        and readable, its contents are simply not valid YAML. The
        loader returns ``Err([ValidationError])`` with line/column
        information; the command maps that to 1 like any other
        validation failure.
        """
        manifest = tmp_path / "broken.yaml"
        manifest.write_text("id: [unclosed\n", encoding="utf-8")
        assert validate(path=manifest) == ExitCode.USER_ERROR

    def test_missing_template_file_returns_user_error(self, tmp_path: Path) -> None:
        """A blueprint whose template does not exist exits 1.

        The loader's filesystem phase catches this (it is the last
        phase, and it is why the loader's contract covers "usable",
        not just "well-formed"). A blueprint that names a template
        it does not ship is invalid in the sense that matters to
        the user: ``forge create`` would fail on it.
        """
        # A blueprint that references a template it does not ship.
        body = """\
schema_version: "1.0.0"
id: sample.missing-template
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
        assert validate(path=manifest) == ExitCode.USER_ERROR


# -----------------------------------------------------------------------------
# 2 — BLUEPRINT_ERROR
# -----------------------------------------------------------------------------
class TestBlueprintErrorExitCode:
    """A directory without a manifest exits 2.

    Exit 2 is a *usage* error: the caller pointed ``forge validate``
    at something that is not a blueprint directory. The blueprint was
    never loaded, so there is no blueprint error to report — the
    invocation itself was wrong. Keeping this distinct from exit 1
    means a CI job that treats exit 1 as "print the errors" does not
    print an empty error list when the caller typo'd the path.
    """

    def test_directory_without_manifest_returns_blueprint_error(self, tmp_path: Path) -> None:
        """A directory with no ``blueprint.yaml`` exits 2.

        The command body detects the missing manifest before the
        loader runs — the loader would (correctly) report a
        file-not-found, but that message is less specific than the
        one the command can produce: "the directory you named is not
        a blueprint directory".
        """
        empty = tmp_path / "not-a-blueprint"
        empty.mkdir()
        assert validate(path=empty) == ExitCode.BLUEPRINT_ERROR


# -----------------------------------------------------------------------------
# 5 — INTERNAL_ERROR
# -----------------------------------------------------------------------------
class TestInternalErrorExitCode:
    """A loader crash exits 5, not 1 — because it is a Forge bug.

    The loader is designed to be total: every *expected* failure is a
    returned ``Err``. An exception that escapes it is, by
    construction, a bug in Forge. Per ADR-0013 that maps to exit 5
    (``INTERNAL_ERROR``), not to exit 1 (``USER_ERROR``). The user
    cannot fix a bug in Forge; telling them "your blueprint is
    invalid" would waste their time and hide ours.

    The test simulates the crash by monkeypatching the loader class,
    which is the only way to reach this branch without shipping a
    real bug. See the test's own docstring for the exact reasoning.
    """

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
        # ``--debug`` is off; the command converts the exception to a
        # user-facing internal-error report and returns 5. It does
        # *not* re-raise (that path is covered in test_validate_paths).
        monkeypatch.setattr(validate_module, "_debug_requested", lambda: False)

        assert validate(path=manifest) == ExitCode.INTERNAL_ERROR


# -----------------------------------------------------------------------------
# Sanity: the exit-code enum is what we think it is
# -----------------------------------------------------------------------------
class TestExitCodeEnum:
    """Pin the four values this command emits.

    ADR-0013's contract for this command has four values. Pinning them
    here — not as magic numbers in assertions, but as named members —
    means that a change to ``ExitCode``'s numeric values fails a test
    at the exact place a reader would look for the contract. A
    contributor cannot "clean up" the enum without seeing this test
    fail and understanding the consequence.
    """

    def test_contract_values(self) -> None:
        assert ExitCode.SUCCESS == 0
        assert ExitCode.USER_ERROR == 1
        assert ExitCode.BLUEPRINT_ERROR == 2
        assert ExitCode.INTERNAL_ERROR == 5
