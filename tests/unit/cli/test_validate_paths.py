"""Tests for `forge validate` path handling.

Scope
-----
These tests prove the **path-resolution contract** of the `validate`
command in isolation from Typer's argument parser. They call the
command's callback function directly, bypassing the CLI, so a failure
here means the command body's logic is wrong — not that Typer rendered
an argument error differently in a new release.

Why not `CliRunner`
-------------------
Typer's argument validators (`exists=True`, `readable=True`,
`dir_okay=True`, `file_okay=True`, `resolve_path=True`) run *before*
the callback is invoked and are already exercised by Typer's own
suite. What we own — and what these tests own — is the code that
runs *inside* the callback:

    * `_resolve_manifest` choosing between a file and a directory,
    * the "directory without a blueprint.yaml" case returning the
      correct exit code (2, not 1) with an actionable message,
    * the "file path passed directly" case being treated as the
      manifest rather than re-joined with `blueprint.yaml`,
    * the source label rendered in output being the resolved path,
      not the raw user input (stability across `cwd`).

Every one of those is a pure function of the path — no TTY, no
environment, no `sys.argv` — which is exactly what makes them fast
and deterministic.

Why every call goes through ``_exit_code``
------------------------------------------
``validate`` raises ``typer.Exit`` on every exit path — the mechanism
Typer documents for exit codes under ``standalone_mode=False``. The
helper ``tests.unit.cli._validate_helpers._exit_code`` unwraps that
raise into an ``int`` so the test bodies can continue to assert on
the exit code as a value. Every test that asserts on the code, or
that merely wants to swallow the raise, uses it.

The one exception is ``test_loader_exception_under_debug_is_reraised``,
which deliberately uses ``pytest.raises`` because its subject *is*
the mechanism — "under ``--debug``, the exception propagates
unchanged, and its type and message are preserved". Wrapping that in
``_exit_code`` would hide what the test is asserting.

Assertion discipline: whitespace-insensitive matching
-----------------------------------------------------
The `ui` layer (or any future emit layer) may wrap long messages at
a terminal width. Wrapping a path is a *display* concern and its
exact break positions are not part of any command's contract — but
it does mean a naive ``assert "/tmp/x/y" in output`` fails when the
wrapper inserts a newline mid-token.

The assertions below therefore compare against whitespace-stripped
output. That is the *correct* contract: "the information the user
needs appears in the output." It is not a concession to a buggy
wrapper; it is a refusal to let the tests depend on a presentational
detail (line breaks) that the commands themselves do not promise.

If/when the `ui` layer stops wrapping (see ADR-0012 — renderers and
emit helpers must not wrap), these assertions still pass: stripping
whitespace from unwrapped text is a no-op for the substrings tested
here. Nothing needs to change.

Fixtures
--------
Tests build their own tiny blueprint trees in `tmp_path` rather than
sharing a `tests/fixtures/validate/` directory. Two reasons:

    1. **Locality.** A reader of a failing test sees the exact bytes
       that produced the failure, on the same screen, without opening
       another file.
    2. **Isolation.** Tests that share fixtures can accidentally
       depend on each other's mutations. `tmp_path` is fresh per
       test, so a change to one test cannot silently alter another.

The one place we *do* share a fixture (a known-valid blueprint body)
is factored into a module-level helper, because three tests need it
and duplicating it three times would be worse than sharing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forge.cli.commands.validate import _resolve_manifest, validate
from forge.core.exceptions import ExitCode
from tests.unit.cli._validate_helpers import _exit_code

# -----------------------------------------------------------------------------
# Whitespace-insensitive matching helper
# -----------------------------------------------------------------------------
# Centralised so the rationale (see module docstring) lives in one
# place. Every test that inspects rendered output uses it, so the
# suite cannot accidentally drift into "some tests require unwrapped
# output, some do not". That drift is exactly what breaks a test
# suite when a wrapper's width changes.
_WS = re.compile(r"\s+")


def _compact(text: str) -> str:
    """Return `text` with every run of whitespace collapsed to nothing.

    Collapsing to *empty* (not to a single space) is deliberate: it
    makes ``"/a/b"`` match across a line break that splits the word
    at any character. Collapsing to a single space would only match
    breaks between word boundaries — which is not how a fixed-width
    wrapper breaks long tokens.

    The function name and behaviour mirror what the tests need to
    express: "does this token appear in the output, regardless of
    how the emit layer arranged it on lines."
    """
    return _WS.sub("", text)


# -----------------------------------------------------------------------------
# Shared valid blueprint body
# -----------------------------------------------------------------------------
_VALID_BLUEPRINT = """\
schema_version: "1.0.0"
id: sample.valid
name: Valid Blueprint
version: 1.0.0
description: Minimal blueprint used only in tests.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""


def _write_blueprint(directory: Path, body: str = _VALID_BLUEPRINT) -> Path:
    """Write a blueprint.yaml inside `directory` and return its path.

    Centralised so tests cannot disagree about the manifest filename.
    If `blueprint.yaml` ever changes, only this helper and the
    `_resolve_manifest` implementation change -- every test still
    works.
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "blueprint.yaml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


# -----------------------------------------------------------------------------
# `_resolve_manifest` — pure path resolution
# -----------------------------------------------------------------------------
# These tests target the helper directly. They are the fastest and
# clearest way to prove the resolution rules; the command-level tests
# below then verify that the resolved path is actually used.
class TestResolveManifest:
    """Direct tests of `_resolve_manifest`'s file-vs-directory logic."""

    def test_file_path_is_returned_unchanged(self, tmp_path: Path) -> None:
        """A file the caller named is the manifest — do not re-join.

        This is the "user passed `./my-blueprint/blueprint.yaml`"
        case. The loader will parse *that* file; the resolver must
        not append another ``blueprint.yaml``.
        """
        manifest = _write_blueprint(tmp_path / "bp")
        assert _resolve_manifest(manifest) == manifest

    def test_directory_with_manifest_returns_the_manifest(self, tmp_path: Path) -> None:
        """A directory containing blueprint.yaml resolves to that file.

        This is the "user passed `./my-blueprint/`" case — the
        primary UX for blueprint authors.
        """
        manifest = _write_blueprint(tmp_path / "bp")
        assert _resolve_manifest(manifest.parent) == manifest

    def test_directory_without_manifest_returns_none(self, tmp_path: Path) -> None:
        """A directory with no blueprint.yaml returns None, not a guess.

        Returning None (rather than an unrelated file or a raise) is
        what lets the command body treat "wrong folder" as a single
        branch. Typer has already verified the directory exists and
        is readable; the only question left is *whether it is a
        blueprint*.
        """
        empty = tmp_path / "not-a-blueprint"
        empty.mkdir()
        assert _resolve_manifest(empty) is None

    def test_directory_with_manifest_named_differently_is_rejected(self, tmp_path: Path) -> None:
        """Only `blueprint.yaml` counts — not `Blueprint.yaml`, not YAML.

        The manifest filename is part of the contract (see the loader's
        `MANIFEST_NAME`). A test that accepted any `.yaml` would
        silently permit typos and case-mismatches that the loader would
        then reject with a *worse* message ("no blueprint.yaml found
        at …") three layers down. Failing here keeps the message close
        to the mistake.
        """
        directory = tmp_path / "wrong-name"
        directory.mkdir()
        (directory / "Blueprint.yaml").write_text(_VALID_BLUEPRINT)
        assert _resolve_manifest(directory) is None


# -----------------------------------------------------------------------------
# `validate` callback — end-to-end path handling, without Typer
# -----------------------------------------------------------------------------
class TestValidateCallbackPathHandling:
    """`validate` accepts a file path and a directory path."""

    def test_file_path_on_valid_blueprint_returns_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Passing the manifest directly returns exit 0 and a success line.

        Proves the file-path branch reaches the loader with the
        correct argument (the file itself) and that the loader's
        success path is rendered.
        """
        manifest = _write_blueprint(tmp_path / "bp")
        code = _exit_code(lambda: validate(path=manifest, json=False))

        assert code == ExitCode.SUCCESS
        captured = capsys.readouterr()
        rendered = captured.out + captured.err
        # ``compact`` handles any wrapping the ui layer applies.
        assert "OK:sample.validv1.0.0" in _compact(rendered)

    def test_directory_path_on_valid_blueprint_returns_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Passing the directory returns exit 0 — same result as the file.

        Proves the directory branch resolves to the manifest inside
        the directory, rather than passing the directory to the loader
        (which would fail with "cannot read file: Is a directory" on
        POSIX).
        """
        _write_blueprint(tmp_path / "bp")
        code = _exit_code(lambda: validate(path=tmp_path / "bp", json=False))

        assert code == ExitCode.SUCCESS
        captured = capsys.readouterr()
        rendered = captured.out + captured.err
        assert "OK:sample.validv1.0.0" in _compact(rendered)

    def test_directory_without_manifest_returns_blueprint_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A directory with no manifest is exit 2, not exit 1.

        Exit 2 is a *usage* error: the caller pointed at something
        that is not a blueprint directory. Exit 1 is reserved for
        "the blueprint you pointed at is invalid". Scripts that
        branch on ``$?`` must be able to tell the two apart.

        The assertion checks two facts:
            1. The message names the missing manifest.
            2. The message names the directory the user passed,
               so they know *which* input was wrong.

        Both are checked on whitespace-stripped output because the
        emit layer may wrap either token.
        """
        empty = tmp_path / "not-a-blueprint"
        empty.mkdir()
        code = _exit_code(lambda: validate(path=empty, json=False))

        assert code == ExitCode.BLUEPRINT_ERROR  # 2
        err = capsys.readouterr().err
        compact = _compact(err)

        assert "Noblueprint.yamlfound" in compact
        # ``empty`` is the directory name the user passed; its
        # presence in the output is the actionable part of the
        # message. The full absolute path is not asserted -- a
        # wrapper may break it and a future "trim to cwd" change
        # would legitimately shorten it.
        assert _compact(str(empty)) in compact

    def test_invalid_blueprint_from_file_returns_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed manifest returns exit 1 with the loader's errors.

        This is the boundary between "wrong path" (2) and "wrong
        content" (1). The file is found; the *contents* are what
        failed. One exit code per user mistake.
        """
        manifest = tmp_path / "broken.yaml"
        manifest.write_text("id: [unclosed\n", encoding="utf-8")

        code = _exit_code(lambda: validate(path=manifest, json=False))

        assert code == ExitCode.USER_ERROR  # 1
        captured = capsys.readouterr()
        compact = _compact(captured.out + captured.err)
        assert "validationerror" in compact.lower()

    def test_invalid_blueprint_from_directory_returns_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same as above but through the directory path.

        Ensures the directory branch does not alter exit-code
        behaviour: both branches must return the loader's verdict
        unchanged.
        """
        _write_blueprint(tmp_path / "bp", body="id: [unclosed\n")

        code = _exit_code(lambda: validate(path=tmp_path / "bp", json=False))

        assert code == ExitCode.USER_ERROR  # 1


# -----------------------------------------------------------------------------
# Debug flag — path handling does not change, but exception handling does
# -----------------------------------------------------------------------------
class TestValidateDebugBehaviour:
    """`--debug` re-raises loader exceptions; without it, they exit 5."""

    def test_loader_exception_returns_internal_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A loader crash becomes exit 5, not exit 1, by default.

        The loader is designed to be total: any exception that
        escapes it is a Forge bug, and per ADR-0013 the exit code for
        "the tool is broken" is 5. Collapsing it into 1 would tell
        the user their blueprint is at fault when the tool is.

        The test forces the loader to raise (by monkeypatching the
        loader class), because making a real blueprint crash the
        loader would require either a bug or a monkeypatched schema —
        both of which are worse than the monkeypatch here.
        """
        from forge.cli.commands import validate as validate_module

        manifest = _write_blueprint(tmp_path / "bp")

        class _BoomLoader:
            def load(self, *_args, **_kwargs):
                raise RuntimeError("simulated loader crash")

        monkeypatch.setattr(validate_module, "BlueprintLoader", _BoomLoader)
        # Ensure `--debug` is NOT on the argv, so the default path runs.
        monkeypatch.setattr(validate_module, "_debug_requested", lambda: False)

        code = _exit_code(lambda: validate(path=manifest, json=False))

        assert code == ExitCode.INTERNAL_ERROR  # 5
        err = capsys.readouterr().err
        compact = _compact(err).lower()

        # The user-facing message must name the failure as *internal*
        # (their fault: no), point at ``--debug`` (escape hatch:
        # yes), and name the issue tracker (report: yes).
        assert "internalforgeerror" in compact
        assert "--debug" in compact
        assert "forge-cli/issues" in compact

    def test_loader_exception_under_debug_is_reraised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With `--debug`, the exception propagates for `main()` to render.

        A contributor diagnosing the loader needs the traceback; the
        command must not swallow it. `main()` catches it via
        `handle_exception(debug=True)`, which re-raises.

        This test deliberately does *not* use ``_exit_code`` — its
        subject is the raise itself ("the exception propagates
        unchanged"), not the code it would produce. ``pytest.raises``
        is the correct tool here: it asserts on the mechanism the
        command is documented to use under ``--debug``. Wrapping the
        call in ``_exit_code`` would catch the raise and hide what
        the test is asserting.
        """
        from forge.cli.commands import validate as validate_module

        manifest = _write_blueprint(tmp_path / "bp")

        class _BoomLoader:
            def load(self, *_args, **_kwargs):
                raise RuntimeError("simulated loader crash")

        monkeypatch.setattr(validate_module, "BlueprintLoader", _BoomLoader)
        monkeypatch.setattr(validate_module, "_debug_requested", lambda: True)

        with pytest.raises(RuntimeError, match="simulated loader crash"):
            validate(path=manifest, json=False)


# -----------------------------------------------------------------------------
# Source label — the resolved path appears in the output, not the raw input
# -----------------------------------------------------------------------------
class TestValidateSourceLabel:
    """The rendered source label reflects the resolved manifest, not the input.

    Why this matters: if a user runs ``forge validate ./bp/``, the
    error output must name the file it actually read
    (``./bp/blueprint.yaml``), not the directory they typed. Naming
    the raw input would hide *which file* failed, which is the single
    most useful piece of information in a validation report.
    """

    def test_directory_input_names_the_manifest_in_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Directory input → the resolved manifest path appears in errors.

        We use an invalid blueprint so the source label is rendered;
        a valid blueprint's success line does not include the path.
        """
        _write_blueprint(tmp_path / "bp", body="id: [unclosed\n")

        _exit_code(lambda: validate(path=tmp_path / "bp", json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        # ``blueprint.yaml`` is the distinguishing token. Asserting
        # on whitespace-stripped output keeps the test independent of
        # any wrapping the emit layer applies.
        assert "blueprint.yaml" in _compact(rendered)

    def test_file_input_names_the_file_in_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """File input → the exact file the user typed appears in errors."""
        manifest = tmp_path / "broken.yaml"
        manifest.write_text("id: [unclosed\n", encoding="utf-8")

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        assert "broken.yaml" in _compact(rendered)
