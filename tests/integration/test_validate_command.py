"""Integration tests for `forge validate` — the binary as a black box.

Scope
-----
These tests spawn ``python -m forge.cli validate ...`` as a
subprocess and assert on its **observable behaviour**: exit code,
stdout, stderr. They are the *only* tests in the suite that exercise
the process-level contract that CI scripts, editors, and shell
pipelines actually depend on.

The unit suite (``tests/unit/cli/test_validate_*.py``) calls the
callback directly. That is the right level for verifying *logic*:
fast, deterministic, and failures point at our code. But it does not
verify things that only exist at the process boundary:

    * **The CLI actually dispatches to `validate`.** A unit test can
      call the function; it cannot prove Typer registers it under the
      right name, or that the registry wires it into the root app
      without a name collision.
    * **The exit code reaches `$?`.** A callback's return value is a
      Python ``int``; whether Typer and ``main()`` turn that into the
      process's exit status is a property of the *entry point*, not
      the callback.
    * **Stream separation.** A unit test captures ``stdout`` and
      ``stderr`` through ``capsys``, which merges nothing and
      separates everything by magic. At the process level, we assert
      the same separation but with ``subprocess.run(capture_output=True)``,
      which is what a shell script sees.
    * **Import-side effects.** If ``forge.cli.main`` ever imports
      something that writes to stdout at import time (a stray
      ``print``, a deprecation warning on some dependency), the unit
      tests never see it because they import ``forge`` in a
      controlled context. A subprocess sees it. That is the kind of
      bug this tier exists to catch.

These tests are slower (one Python interpreter startup per case),
which is why there are only four: one per acceptance criterion that
depends on process-level behaviour. Logic-per-mutation tests live in
the unit tier; boundary tests live here.

Which binaries the tests invoke
-------------------------------
Each test invokes ``[sys.executable, "-m", "forge.cli", "validate",
...]`` rather than ``["forge", "validate", ...]``:

    * ``sys.executable`` guarantees the same interpreter and
      environment that is running pytest — a caller with
      ``forge`` on their ``PATH`` but pointing at a different
      install would silently test a different binary.
    * ``-m forge.cli`` is the documented alternate entry point
      (``src/forge/cli/__main__.py``), and it exercises the same
      ``main()`` that the ``forge`` console script uses. Testing
      through it does not weaken the guarantee that ``forge validate``
      works; it strengthens it, because it proves the ``-m`` path
      is a working alternate.

If a reviewer wants to prove ``forge`` (the console script) works,
that is a *packaging* test (does the entry point install?), not a
*command* test. Mixing them would make every command test depend on
``pip install -e .`` having been run, which is a separate concern
covered by the packaging suite.

Determinism guarantees
----------------------
* **No real network.** Every input is a local fixture in
  ``tmp_path``; no test depends on GitHub, PyPI, or a DNS server.
* **No real ``$HOME``.** ``tmp_path`` is used for all writes, and
  the loader reads only the paths the test passes. A test that
  accidentally depended on the developer's ``~/.forge/`` would be a
  bug — and would show up here as a flaky pass/fail on different
  machines.
* **No terminal assumptions.** ``capture_output=True`` means the
  child process sees non-TTY stdout/stderr; ``validate`` does not
  branch on TTY (that is ``create``'s concern), so this is correct
  and stable.

What is not tested here
-----------------------
* **Message wording.** A subprocess test that asserted the exact
  string of an error message would fail every time someone
  rephrased a hint for readability. Messages are the *unit* tests'
  concern; this tier checks that the message is *there* (substring
  match on a stable token) and that it lands on the right stream.
* **`--debug` behaviour.** The full traceback is Python's
  output, not ours; verifying it lands in stderr is enough to be
  covered by ``test_validate_paths.py`` at the unit tier. Re-testing
  it here would mean spawning a subprocess just to check that
  ``traceback.print_exc`` behaves the same way it always has.
* **`--json` shape.** ``test_validate_json.py`` verifies the
  envelope at the unit tier; the shape does not depend on the
  process boundary, only on the renderer. The one JSON integration
  test below asserts the output *parses*, not what it contains —
  that division is deliberate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Fixtures — small, self-contained blueprint trees
# -----------------------------------------------------------------------------
# The integration tier builds its own fixtures (rather than reusing
# ``tests/fixtures/validate/``) because the fixtures in the shared
# tree are *golden artefacts* for the contract snapshot; importing
# them here would create a subtle coupling: a shape change in a
# golden fixture could fail an integration test that has nothing to
# do with shape. Building fresh input per test keeps the tiers
# independent and the tests readable top-to-bottom.

_VALID_BLUEPRINT = """\
schema_version: "1.0.0"
id: sample.valid
name: Valid Blueprint
version: 1.0.0
description: A blueprint used only in integration tests.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""

# Two independent schema violations (missing `id`, malformed
# `version`). Two is the smallest number that proves aggregation at
# the process boundary: one would not distinguish "rendered all
# errors" from "rendered the first error and stopped".
_TWO_VIOLATIONS = """\
schema_version: "1.0.0"
name: Two Violations
version: "1.0"
description: A blueprint with two schema violations.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""


def _write_blueprint(directory: Path, body: str) -> Path:
    """Write ``blueprint.yaml`` inside ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "blueprint.yaml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


def _run_validate(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m forge.cli validate ...`` as a subprocess.

    Returns the completed process with ``capture_output=True`` and
    ``text=True``, so callers can assert on ``returncode``, ``stdout``,
    and ``stderr`` without further decoding. Environment inheritance
    is intentional: on CI, ``PYTHONPATH`` is set correctly by the
    runner, and on a developer machine, the editable install handles
    it. Stripping the environment would require the test to
    reconstruct it, which is worse.
    """
    return subprocess.run(
        [sys.executable, "-m", "forge.cli", "validate", *args],  # noqa: S603
        capture_output=True,
        text=True,
        # Timeouts are deliberately absent: a hang here is a real bug
        # that CI's global timeout should catch, and a per-test timeout
        # would obscure the failure as "a slow test" rather than
        # "the command hangs". If a future change makes any of these
        # tests slow, the fix is to speed up the command, not to add
        # a timeout that hides the slowness.
    )


# -----------------------------------------------------------------------------
# Criterion 1 — valid blueprint, exit 0, success message
# -----------------------------------------------------------------------------
class TestValidBlueprint:
    """A blueprint that validates cleanly is the tool's happy path.

    Everything downstream — CI jobs, editor integrations, pre-commit
    hooks — is built on this case working. If it does not, none of
    the failure cases matter.
    """

    def test_directory_exits_zero_with_success_message(self, tmp_path: Path) -> None:
        """``forge validate <dir>`` on a valid blueprint exits 0.

        This is the *acceptance criterion's canonical invocation*.
        The path is a directory, which is the form blueprint authors
        use most; the message includes the blueprint's identity so a
        user can confirm *which* file was validated when the command
        ran against a directory.
        """
        _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        proc = _run_validate(str(tmp_path / "bp"))

        assert proc.returncode == 0, proc.stderr
        # The success line names the blueprint identity, so a
        # directory validation is verifiable by inspection.
        assert "sample.valid" in proc.stdout
        assert "1.0.0" in proc.stdout
        # No commentary on stderr in the happy path: if the tool
        # succeeded, it should be silent everywhere except stdout.
        assert proc.stderr == ""

    def test_file_path_exits_zero_with_success_message(self, tmp_path: Path) -> None:
        """``forge validate <file>`` on a valid blueprint exits 0.

        The file-path form is what an editor integration would use
        (it knows the exact file it saved). Both forms are the
        documented interface, and both must work identically at the
        process level — a difference here would be a
        process-boundary regression the unit tests could not see.
        """
        manifest = _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        proc = _run_validate(str(manifest))

        assert proc.returncode == 0, proc.stderr
        assert "sample.valid" in proc.stdout
        assert proc.stderr == ""


# -----------------------------------------------------------------------------
# Criterion 2 — invalid blueprint, exit 1, all errors rendered
# -----------------------------------------------------------------------------
class TestInvalidBlueprint:
    """A blueprint that fails validation is the tool's other happy path.

    "Happy path" is not a joke here: a validation tool that *fails
    to fail* is worse than one that crashes, because it silently
    ships broken blueprints. The two tests in this class prove that
    an invalid blueprint produces the process-level signal a CI job
    branches on: nonzero exit, plus every error on stdout/stderr.
    """

    def test_two_violations_exit_one_and_render_both(self, tmp_path: Path) -> None:
        """A blueprint with two violations exits 1 and names both.

        The *count* of rendered errors is a unit concern (see
        ``test_validate_all_errors.py``). What this test proves is
        that at the process boundary, the errors *reach the user*:
        the subprocess's stdout or stderr contains the field names
        for both violations. A regression that dropped one error
        between the loader and the renderer would pass unit tests
        (which call the renderer directly) but fail here (which
        reads what actually reached the pipe).

        Both fields are checked by *name* (``id``, ``version``),
        not by exact message text — the message wording is not part
        of the contract, but "the field is named" is the minimum
        that makes an error actionable.
        """
        _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        proc = _run_validate(str(tmp_path / "bp"))

        assert proc.returncode == 1, (
            f"Invalid blueprint must exit 1, got {proc.returncode}. "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        # Both violations appear somewhere in the output. The
        # human renderer puts the summary on stderr and the detail
        # on stdout, so we search both — a change to which stream
        # gets the message would be a UI change worth noticing, but
        # not one this test needs to pin.
        combined = proc.stdout + proc.stderr
        assert "id" in combined, f"'id' violation missing from output: {combined!r}"
        assert "version" in combined, f"'version' violation missing from output: {combined!r}"

    def test_no_manifest_exits_two_with_usage_message(self, tmp_path: Path) -> None:
        """A directory without ``blueprint.yaml`` exits 2, not 1.

        This is the boundary between *wrong invocation* (2) and
        *wrong content* (1). A shell script that treats exit 1 as
        "print the validation errors" would print nothing for this
        case — because there are no validation errors, there is only
        a directory the user named that is not a blueprint.

        Exit 2 is Typer's own usage-error code, and this test proves
        the command produces the same signal for a usage error as
        Typer does for a missing argument. Two distinct causes,
        one consistent code — which is what makes CI scripts
        tractable.
        """
        empty = tmp_path / "not-a-blueprint"
        empty.mkdir()

        proc = _run_validate(str(empty))

        assert proc.returncode == 2, (
            f"Missing-manifest must exit 2, got {proc.returncode}. "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        # The message names the directory the user passed, so a
        # copy-pasted error is actionable.
        assert "blueprint.yaml" in (proc.stdout + proc.stderr)


# -----------------------------------------------------------------------------
# Criterion 3 — --json produces a parseable envelope
# -----------------------------------------------------------------------------
class TestJsonMode:
    """``--json`` emits a machine-readable envelope on stdout.

    The *shape* of the envelope is tested exhaustively at the unit
    tier (``test_validate_json.py``). At the process boundary the
    relevant question is narrower: does the flag do what it says on
    a real invocation, and does the output parse?

    That is a small question with a large difference: a build that
    passes the unit tests but where ``--json`` is silently ignored
    at the process boundary (because the flag is not registered, or
    because a wrapper strips it) would ship a broken integration for
    every downstream consumer.
    """

    def test_json_mode_emits_parseable_envelope(self, tmp_path: Path) -> None:
        """``forge validate --json <dir>`` emits a parseable JSON document.

        The assertion is deliberately minimal: ``json.loads`` of
        stdout succeeds, and the payload has the top-level keys the
        contract requires. The *values* are unit-tested; here we
        only prove the boundary is intact.
        """
        _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        proc = _run_validate("--json", str(tmp_path / "bp"))

        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == "", (
            f"`--json` wrote to stderr: {proc.stderr!r}. "
            f"In JSON mode all output belongs in the envelope on stdout."
        )
        payload = json.loads(proc.stdout)
        assert payload["valid"] is True
        assert payload["schema_version"] == "1.0.0"

    def test_json_mode_reports_invalid_blueprint_in_envelope(self, tmp_path: Path) -> None:
        """``--json`` on an invalid blueprint exits 1 and lists errors.

        This is what a CI job sees: exit 1 (so the job fails), a
        parseable envelope (so the job can render its own diagnostic
        instead of scraping text), and no stderr noise (so the log
        stays clean).

        Both signals matter:
            * exit code = "did the run succeed?"
            * envelope   = "why did it fail?"

        A tool that gave only one of those would force every consumer
        to reconstruct the other.
        """
        _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        proc = _run_validate("--json", str(tmp_path / "bp"))

        assert proc.returncode == 1, (
            f"Invalid blueprint must exit 1 even with --json, got "
            f"{proc.returncode}. stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert (
            proc.stderr == ""
        ), f"`--json` on an invalid blueprint wrote to stderr: {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["valid"] is False
        assert len(payload["errors"]) == 2
