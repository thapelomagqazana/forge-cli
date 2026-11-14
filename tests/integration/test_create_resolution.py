"""End-to-end integration tests for ``forge create``'s resolution pipeline.

These tests exercise the *whole* path a real command takes, from argv
through the CLI, through the command module, through the resolver,
and back out to a rendered exit code. They are the *integration*
counterpart to the unit tests in ``tests/unit/resolution/`` and
``tests/unit/cli/test_create_dual_mode.py``.

Scope
-----
Integration tests are expensive: they spawn a subprocess, import the
whole package, load real blueprints, and exercise real PyYAML parsing
and real Pydantic validation. That cost is justified only when the
test is verifying something that *cannot* be verified at a lower
level — specifically:

* **The dispatcher works.** That ``forge create …`` reaches the
  command, that the command reaches the resolver, and that the
  resolver's ``Err`` becomes a non-zero exit code with the expected
  rendering. Unit tests stub each stage; only a full run proves the
  wiring.
* **The exit code is the shell contract.** A script branching on
  ``$?`` needs the actual process exit code, not a Python-level
  exception object. Only ``subprocess.run`` gives us that.
* **The streams are separated.** A user piping stdout to a file and
  reading stderr on the terminal relies on ``handle_exception``
  writing to stderr and success output going to stdout. Only a
  subprocess run can prove the separation, because ``CliRunner``
  captures both and mixes them.
* **The public CLI surface is stable.** Users invoke
  ``forge create <blueprint> --name <name>``, not
  ``VariableResolver().resolve(...)``. Only the subprocess run
  proves the surface they actually use.

Anything that can be checked without spawning a process is tested
in the unit layer. This file does **not** re-test:

* Which source wins a variable (``test_precedence.py``).
* Whether required variables error (``test_required.py``).
* Coercion spelling (``test_type_coercion.py``).
* Named validator rules (``test_project_name.py`` etc.).
* The wizard's prompt loop (``test_create_dual_mode.py``).

Those tests are fast because they are unit-scoped. If this file
duplicated them, it would multiply the cost of the whole suite for
no new coverage. The split is enforced by the module docstring's
"scope" section — a contributor adding a test here should first ask
"does this test need to spawn a subprocess?" If the answer is "no",
the test belongs in the unit layer.

Why subprocess and not ``CliRunner``
------------------------------------
``CliRunner.invoke(forge_app, ...)`` is fast and gives us structured
exceptions via ``result.exception``. It is the right tool for
testing the parser and the command wiring (which is what
``test_create_dual_mode.py`` uses it for).

``CliRunner`` has three limits that matter here:

1. **It does not exercise ``main()``.** ``handle_exception`` lives in
   ``main()``; ``CliRunner`` calls ``create_app()`` directly, so
   domain errors propagate as exceptions rather than being rendered.
   That is fine for unit-level assertions on exception *type*, but
   it is exactly the thing users *never see* — the shell goes
   through ``main()``, so the actual user experience is only
   visible end-to-end.

2. **It mixes stdout and stderr.** ``result.output`` combines them.
   A test that wants to assert "the error message goes to stderr and
   stdout stays clean" cannot do that with ``CliRunner``.

3. **It does not give us the process exit code.** ``result.exit_code``
   is set by the harness, and its exact value depends on the Typer
   version's exception-handling logic. The *shell* exit code is what
   a script branching on ``$?`` sees. Only ``subprocess.run`` gives
   us the number the shell would see.

So: ``CliRunner`` for the unit tests, ``subprocess.run`` here.
Each tool is used for what it is best at.

Hermeticity
-----------
These tests are hermetic in the sense that they do not depend on the
developer's working directory, the developer's ``$HOME``, or any
user-installed blueprints. Isolation comes from *two* mechanisms,
and only two:

1. **``cwd=tmp_path``.** Every run happens in a fresh temp
   directory. ``forge create``'s discovery layer scans
   ``<cwd>/.forge/blueprints/`` (ADR-0009); with a fresh ``tmp_path``
   that directory does not exist by default, so the discovery
   service sees only the bundled blueprints. Tests that *want* to
   exercise discovery of a local blueprint create the directory
   themselves.

2. **Explicit ``--output``.** The destination is passed as an
   absolute path under ``tmp_path``. The command never accidentally
   writes into the source tree, and ``test_create_does_not_write_
   outside_output`` can assert containment using a filesystem
   snapshot.

Earlier versions of this file also set ``FORGE_BLUEPRINT_DIR`` via
an ``isolated_env`` fixture. That variable is not read by the current
discovery layer (ADR-0009 uses ``cwd/.forge/blueprints``, not an
env override), so the fixture was a *silent no-op* — it looked like
it enforced isolation, but only ``cwd`` actually did. The fixture
has been removed. When (if) the discovery layer grows an env
override, it will arrive with its own test in the discovery test
suite proving the override is honoured, and this file can then use
it *knowingly* rather than aspirationally.

Subprocess coverage isolation
-----------------------------
``pytest-cov`` injects ``COV_CORE_*`` environment variables into the
pytest process. If ``run_create`` copies ``os.environ`` verbatim into
the subprocess, those variables leak — the subprocess activates the
coverage sitecustomize and writes its own ``.coverage.<host>.<pid>``
file. That file is written with the subprocess's config, which is
not the parent's (branch measurement on/off, source paths, etc.), and
when ``pytest-cov`` tries to combine the two at teardown, coverage
raises ``DataError: Can't combine statement coverage data with
branch data`` and aborts the whole session with ``INTERNALERROR``.

That failure mode is silent during development (no coverage plugin,
no leak) and only manifests in the full ``make test`` run with
coverage enabled — which is exactly when a failing run is most
expensive to diagnose.

The fix is to strip those variables in ``run_create``. The
subprocess is not part of the unit under test; it is a reproduction
of what a user does. Its coverage is irrelevant, and letting it
participate in the parent's measurement both distorts the metrics
and breaks the combine.

Stream-separation contract for success
--------------------------------------
A successful ``forge create`` writes:

* **stdout**: the success message (names the project).
* **stderr**: progress lines (``[1/11] copy .gitignore`` etc.),
  which the pipeline emits per-file for the user's benefit
  (ADR-0019's "progress is commentary, not product").

That is *by design*: a script that pipes stdout to a file and treats
stderr as the user's console sees both the product and the
commentary, correctly separated. An earlier version of the success
test asserted ``stderr == ""`` — that assertion was written against
a pre-pipeline mental model in which the CLI was silent on success.
Once the pipeline landed, its progress lines (correctly) go to
stderr, and the assertion had to be relaxed from "stderr is empty"
to "stderr contains no *error* lines".

The distinction the tests now enforce is:

* **Errors** on stderr always begin with ``✗`` (see
  ``forge.cli.ui.print_error``). A script grepping for ``^✗`` sees
  exactly the failures.
* **Progress** lines on stderr begin with ``  [N/M]`` (see
  ``forge.cli.ui.print_progress``). They are commentary, not
  failures; a shell script should ignore them.
* **Success** on stdout names the project. It is the only line a
  script that wants to confirm success needs to look for.

The tests in this file assert on those three properties, not on
exact wording. A UX change to the success message (say, from
"Created 'my-api'" to "Project my-api created") keeps the tests
green — correctly, because the *contract* ("success output names
the project") is unchanged. A change that broke the contract (say,
printing failures to stdout instead of stderr) would fail
immediately.

Asserting on *what users rely on*, not on copy
----------------------------------------------
Several assertions in this file check stderr for the *content* of a
message. They deliberately do **not** pin exact wording, because
wording is a UX concern that may legitimately change without
breaking any user-facing contract. What users rely on, and what the
tests check, are three things:

1. **The field name.** A script that greps stderr for ``"--name"``
   or ``"project name"`` is relying on those tokens appearing. That
   is the shell contract, not the wording of the sentence around
   them.

2. **Rule specificity.** For an invalid name, the message names the
   *rule* that failed (``"lowercase"``, ``"start with a letter"``,
   ``"end with a hyphen"``, etc.), not a generic "invalid". This is
   what distinguishes a helpful error from a dead end, and the
   tests assert on the *set* of acceptable rule phrases rather than
   on any one exact sentence. A future message rewrite that keeps
   the rule names passing; a rewrite that collapses them all to
   "invalid" failing — which is exactly what should happen.

3. **Distinctness.** Different failure modes produce *different*
   messages on stderr, so a user (or a shell script) can tell them
   apart without parsing structured output. This is stronger than
   any single phrase check and is asserted structurally (the two
   stderr strings differ).

Pinning exact wording would make every UX copy change a test
change — the classic "tests as a straightjacket" pattern. The
current assertions are the *contract*: they will keep passing for
any wording that preserves the field names and rule specificity,
and they will fail the moment a regression actually harms users.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_create(
    *args: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run ``forge create`` as a subprocess and return the result.

    The command is invoked as a module (``python -m forge.cli``) so
    the test does not depend on the ``forge`` console-script being
    installed on ``PATH``. That is important for two reasons:

    * **Portability.** ``python -m forge.cli`` works under
      ``uv run``, a plain virtualenv, and a source checkout alike.
      The console script only works when the package has been
      installed with an entry point — which is *usually* true but
      not *always* true in CI, and *never* true during a
      ``pip install -e .`` of a project that has not yet wired up
      its ``[project.scripts]`` section.

    * **Diagnosis.** When the test fails, ``sys.executable -m
      forge.cli`` is a command the developer can run *directly*
      to reproduce the failure in their own shell. The console
      script's ``forge`` might be a different installation,
      confusing the reproduction.

    Environment
    -----------
    The subprocess inherits the current environment **except** for
    ``COV_CORE_*`` variables. Those are injected by ``pytest-cov``
    into the pytest process and cause the subprocess to activate
    the coverage sitecustomize and write its own ``.coverage.*``
    file. That file's schema (branch vs statement measurement) may
    differ from the parent's, and the combine step at teardown
    raises ``DataError``, which aborts the whole run with
    ``INTERNALERROR``. Stripping them keeps the subprocess from
    polluting the parent's coverage data; see the module docstring's
    "Subprocess coverage isolation" section.

    Parameters
    ----------
    *args
        CLI arguments after ``create``. Most tests pass
        ``"python.fastapi.api", "--name", "my-api", ...``.
    cwd
        Working directory for the subprocess. Should be ``tmp_path``
        (or a subdirectory of it) so the run is hermetic.

    Returns
    -------
    ``subprocess.CompletedProcess`` with ``returncode``,
    ``stdout``, and ``stderr`` all populated. Text mode is on, so
    all three are ``str``.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}

    # The command list is built entirely from ``sys.executable``,
    # a constant module string, and the caller's ``*args`` -- which
    # in every call site are string literals authored in this test
    # file. There is no path by which untrusted input (env vars,
    # user data, network payloads) reaches this argv. ``shell=False``
    # (the default) means no shell metacharacter interpretation, and
    # each element is passed to the interpreter as a discrete argv
    # entry, so S603's "check for execution of untrusted input"
    # concern does not apply here. The suppression is scoped to this
    # call and documented rather than module-wide so a future
    # subprocess call that *does* interpolate external input is
    # still flagged.
    return subprocess.run(
        [sys.executable, "-m", "forge.cli", "create", *args],  # noqa: S603
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,  # we assert on the return code, not the exception
    )


def _error_lines(stderr: str) -> list[str]:
    """Return the lines of ``stderr`` that are *error* messages.

    The CLI's error renderer (``forge.cli.ui.print_error``) prefixes
    every error line with ``✗``. Progress lines (``forge.cli.ui.
    print_progress``) prefix with ``  [N/M]``. The distinction is the
    "product vs. commentary" contract the CLI enforces.

    This helper is the *one* place that filters by that prefix, so a
    future change to the prefix (should it ever need to change) is a
    one-line fix here, not a scatter of string comparisons in test
    bodies. It is also the semantic anchor for every test that says
    "the run failed / succeeded" — the test asserts on the *error
    lines*, not on the raw stderr, because raw stderr contains
    commentary that is not part of the success-or-failure contract.
    """
    return [line for line in stderr.splitlines() if line.startswith("✗")]


# ---------------------------------------------------------------------------
# AC 1: valid input via flags succeeds with a clean stdout/stderr split
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_valid_non_interactive_create_succeeds(tmp_path: Path) -> None:
    """A well-formed ``forge create`` exits 0 with the project on stdout.

    This is the *integration* form of the acceptance criterion "the
    command accepts a valid invocation". The unit test proves the
    parser accepts the shape; this test proves the *whole process*
    accepts it: no uncaught exception from an import, no stdout
    pollution from a dependency, no crash from a template that
    forgot to close a file. Those failure modes are invisible to
    unit tests because unit tests stop at the resolver.

    Stream contract on success
    --------------------------
    A successful run writes two things:

    * **stdout** — the success message, which names the project.
      This is the *product*: the shell script that wants to confirm
      success needs only look here.
    * **stderr** — progress lines, emitted per-file as the staging
      engine renders and commits the plan (see ADR-0019). These are
      *commentary*: a user watching their terminal sees them; a
      script grepping for errors ignores them.

    An earlier version of this test asserted ``stderr == ""``. That
    was correct when the pipeline was a stub; once the pipeline
    landed, its progress lines (correctly) went to stderr, and the
    assertion had to be relaxed from "stderr is empty" to "stderr
    contains no *error* lines". The relaxed form is what a script
    actually relies on (``grep '^✗'`` sees failures, not commentary),
    and it is what this test now enforces.

    Why the assertion checks the error-line set is empty rather than
    the raw stderr: progress lines are not errors, and treating them
    as errors would make the CLI unable to ever emit progress without
    also failing its own success test. The correct line between
    "success with commentary" and "success with errors" is drawn by
    the ``✗`` prefix, which the CLI's renderers enforce uniformly.
    """
    result = run_create(
        "python.fastapi.api",
        "--name",
        "my-api",
        "--output",
        str(tmp_path),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"create exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    # stdout is where the *product* goes: success, in a form a script
    # can confirm without parsing commentary.
    assert "my-api" in result.stdout, (
        f"success output does not name the project\n" f"--- stdout ---\n{result.stdout}"
    )

    # stderr is where commentary (progress) and errors go. Progress
    # is expected; errors are not. The `_error_lines` helper is the
    # single place that defines the "error" prefix, so a future
    # change to how errors are rendered (say, a `✗` prefix becoming
    # a `[ERROR]` prefix) is a one-line change there, not a scatter
    # of string comparisons across the test suite.
    errors = _error_lines(result.stderr)
    assert not errors, (
        f"successful run emitted error lines on stderr: {errors!r}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    # Sanity: the project actually landed on disk. A run that exits 0
    # but writes nothing is a different class of bug — one that a
    # pure "returncode + streams" assertion would not catch — and it
    # is worth failing loudly on. This is a *light* assertion; the
    # full project-content check lives in
    # ``tests/integration/test_staging_end_to_end.py``. Here we only
    # verify that the file the blueprint's ``verification:`` block
    # names as ``required_file`` exists, which is the minimum a
    # successful create promises.
    assert (tmp_path / "my-api" / "pyproject.toml").is_file(), (
        "successful create did not produce pyproject.toml — the "
        "run reported success but wrote nothing"
    )


# ---------------------------------------------------------------------------
# AC 2: missing required flag fails with exit 1 and names the flag
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_missing_name_fails_with_exit_1(tmp_path: Path) -> None:
    """Missing ``--name`` in non-interactive mode → exit 1, ``--name`` on stderr.

    The exit code is the shell contract: a script that branches on
    ``$?`` treats 1 as "the invocation was wrong" and does not
    retry. A crash-with-traceback (exit 1 by coincidence, but with
    a stack trace) is indistinguishable from a *bug* on a grep of
    stderr, which is a worse user experience than a clean message.

    Two assertions, each load-bearing:

    1. ``returncode == 1`` — the exit-code contract from
       ``ExitCode.USER_ERROR``.
    2. ``"--name" in stderr`` — the message names the missing
       flag, so the user can fix it without reading docs. The
       check is intentionally on ``--name`` (the flag) rather than
       ``name`` (the field), because the user typed the flag.

    Why this test does not need a pipeline marker: the missing-flags
    check runs in ``_resolve_variables``, which is invoked *before*
    ``_generate``. A ``MissingRequiredFlagsError`` is raised and
    rendered — the generator is never reached. The test exercises
    working code and is green.
    """
    result = run_create(
        "python.fastapi.api",
        "--output",
        str(tmp_path),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert result.returncode == 1, (
        f"expected exit code 1, got {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert "--name" in result.stderr
    # The success path prints the plan on stdout; the failure path
    # must not print anything to stdout. A script that captures
    # stdout for later inspection should not receive a partial
    # "plan" from a command that failed.
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# AC 3: invalid name fails during resolution
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_invalid_project_name_fails_with_hint(tmp_path: Path) -> None:
    """A syntactically invalid name is rejected with the rule and a hint.

    This is the end-to-end version of the acceptance criterion
    "invalid project name → error with a hint". The unit tests
    verify the validator returns a hint; this test verifies the
    hint reaches *stderr* in a form the user can act on.

    The specific failure mode this test catches is a pipeline that
    *validates* correctly but *renders* the validation error as a
    Python traceback. In that case the user sees the message but in
    a form they cannot act on, and the exit code may be 1 anyway
    (making the failure invisible to scripts). The end-to-end test
    is the only one that can catch "validation works, rendering
    does not".

    The uppercase name ``"My Project"`` is chosen because it fails
    on *two* rules simultaneously (uppercase and space), so a
    validator that stops after the first failure still produces a
    rejection — the test is not sensitive to which rule fires first,
    only to the fact that the failure is rendered.

    Where the validation lives: ``create.py``'s ``_resolve_variables``
    validates ``project_name`` in the non-interactive branch by
    calling ``_validate_project_name``, which delegates to
    ``resolution.names.project_name``. That is the *same* named
    validator the wizard uses, so a name rejected interactively is
    also rejected non-interactively — and vice versa. Without that
    check the command would proceed to ``_generate``, hit the
    resolver's rejection of the invalid name, and produce the wrong
    exit code and message (exit 5, "unexpected internal error"). The
    test exists to catch that regression — and did, on the run that
    motivated this test file's revision.

    Why the assertion on stderr is on *the field name* and *a
    rule-specific phrase*, not on an exact message: the
    ``project_name`` validator produces different messages per
    failure mode ("project name must be lowercase", "must start
    with a letter", "must not end with a hyphen", and a generic
    "invalid project name" fallback). Pinning the generic phrase
    would fail whenever the validator's rule ordering changes, even
    though the user experience is identical. Pinning the *field
    name* is what a shell script greps for; that is the stable part
    of the contract.
    """
    result = run_create(
        "python.fastapi.api",
        "--name",
        "My Project",
        "--output",
        str(tmp_path),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert result.returncode == 1, (
        f"expected exit code 1, got {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    stderr_lower = result.stderr.lower()

    # 1. The field is named. That is what a script greps for and
    #    what a user scans for when they see the error.
    assert "project name" in stderr_lower or "project_name" in stderr_lower

    # 2. The message names *a rule*, not just the field. Any of
    #    these is acceptable — the validator picks one based on the
    #    input, and "My Project" happens to trip the lowercase check
    #    first. A future reordering (space first) would produce a
    #    different rule phrase, still acceptable, still caught by
    #    the "invalid project name" fallback below.
    rule_phrases = (
        "lowercase",
        "start with a letter",
        "end with a hyphen",
        "invalid project name",
    )
    assert any(
        p in stderr_lower for p in rule_phrases
    ), f"invalid-name stderr did not name a rule; got: {result.stderr!r}"

    # 3. The output is a rendered message, not a traceback.
    assert "Traceback (most recent call last)" not in result.stderr


# ---------------------------------------------------------------------------
# AC 4: --no-interactive overrides a TTY
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_no_interactive_overrides_missing_tty(tmp_path: Path) -> None:
    """``--no-interactive`` works even when stdin is not a TTY (subprocess default).

    ``subprocess.run`` with ``capture_output=True`` and no ``stdin=``
    pipes stdin from the parent — which in a test runner is not a
    TTY. So this test is simultaneously:

    1. A test that ``--no-interactive`` suppresses the wizard.
    2. A test that the command's TTY detection (``is_interactive``)
       correctly falls back to "not interactive" when stdin is a
       pipe.

    Both properties matter. (1) is the flag's documented behaviour;
    (2) is what makes ``forge create`` work in CI, in Docker, in
    ``xargs``, in a shell pipeline — anywhere stdin is not a
    terminal. A regression that broke either would break real
    workflows; testing them together is efficient because they must
    hold *together* for the non-interactive path to work at all.
    """
    result = run_create(
        "python.fastapi.api",
        "--name",
        "my-api",
        "--output",
        str(tmp_path),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"create failed under non-TTY stdin\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert (
        tmp_path / "my-api" / "pyproject.toml"
    ).is_file(), "successful create did not produce pyproject.toml"


# ---------------------------------------------------------------------------
# Hermeticity: local blueprints must not leak into the run
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_local_blueprints_are_ignored(tmp_path: Path) -> None:
    """A ``.forge/blueprints/`` directory in ``cwd`` does not change the outcome.

    Discovery is *supposed* to find local blueprints. This test
    proves the discovery mechanism is *scoped*: a directory in
    ``cwd`` that has nothing to do with the run must not affect
    it. The concern is that discovery might accidentally walk
    ``cwd`` unconditionally and pick up blueprints the user did
    not intend.

    The empty ``.forge/blueprints/`` directory is created here, so
    the test does not depend on the user's actual home directory
    being empty. The fact that it is *empty* is deliberate: a
    non-empty directory would test *discovery*, which is a
    different concern. This test proves that an *empty* local
    blueprints directory is harmless, which is the minimum
    guarantee for ``forge create`` to be predictable.

    Why this is an *integration* test, not a unit test: it proves
    the *process* does not walk the wrong directory. A unit test of
    discovery would prove the discovery service behaves; it would
    not prove that ``forge create`` in a fresh directory with an
    unrelated ``.forge/`` subdir works. The end-to-end path is the
    thing a user hits; the test targets that.
    """
    (tmp_path / ".forge" / "blueprints").mkdir(parents=True)

    result = run_create(
        "python.fastapi.api",
        "--name",
        "my-api",
        "--output",
        str(tmp_path),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"create failed when cwd contained .forge/blueprints/\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert (
        tmp_path / "my-api" / "pyproject.toml"
    ).is_file(), "successful create did not produce pyproject.toml"


# ---------------------------------------------------------------------------
# Exit-code stability across distinct failure categories
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_missing_and_invalid_names_have_distinct_failure_modes(
    tmp_path: Path,
) -> None:
    """Different invalid invocations produce distinguishable failures.

    This is not "distinct exit codes" — both are 1 (user errors).
    It is "distinct *messages* on stderr", so a shell script that
    greps stderr for a category keyword can branch on *which* user
    error it was without parsing structured output.

    The test is worth having end-to-end because "the resolver
    returns distinct codes" is a *unit* property — the question a
    user has is "does the *rendered message* preserve that
    distinction". A rendering layer that collapsed all errors into
    "validation failed" would pass every unit test and still fail
    this one.

    A future ``--json`` flag would make this test redundant (the
    codes would be machine-readable directly). Until then, the
    stderr keyword is what a user has. The test is the contract
    for that keyword.

    Why the assertions are on *what a user relies on* and not on
    exact wording: the ``project_name`` validator returns a
    *different* message per failure mode (uppercase vs. leading
    digit vs. trailing hyphen vs. generic). Pinning the exact
    phrase would fail whenever the rule ordering changes, even
    though the user experience is identical. The assertions here
    check three things the user actually depends on:

    1. The missing case names the flag (``"--name"``).
    2. The invalid case names the field (``"project name"``).
    3. The two messages *differ* — the renderer preserved the
       distinction the resolver produced.
    """
    missing = run_create(
        "python.fastapi.api",
        "--output",
        str(tmp_path / "a"),
        "--no-interactive",
        cwd=tmp_path,
    )
    invalid = run_create(
        "python.fastapi.api",
        "--name",
        "My Project",
        "--output",
        str(tmp_path / "b"),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert missing.returncode == 1
    assert invalid.returncode == 1

    missing_lower = missing.stderr.lower()
    invalid_lower = invalid.stderr.lower()

    # 1. The missing case names the flag.
    assert "--name" in missing_lower

    # 2. The invalid case names the field.
    assert "project name" in invalid_lower or "project_name" in invalid_lower

    # 3. The two messages *differ* — the renderer preserved the
    #    resolver's distinction. This is the load-bearing assertion
    #    the test's name promises; it does not depend on any
    #    specific wording, only on the messages not being
    #    interchangeable.
    assert missing_lower != invalid_lower, (
        f"the two failure modes rendered identically.\n"
        f"missing: {missing.stderr!r}\n"
        f"invalid: {invalid.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test-time guard: no accidental writes into the source tree
# ---------------------------------------------------------------------------


@pytest.mark.integration()
def test_create_does_not_write_outside_output(tmp_path: Path) -> None:
    """A successful run writes only under ``--output``, nowhere else.

    This is the "no side effects on failure / no surprise writes on
    success" guarantee in its integration form. The concern is not
    malicious behavior (nothing in the codebase tries to write
    elsewhere); it is *accidental* writes:

    * A template that accidentally renders to a path relative to
      ``cwd`` instead of ``output``.
    * A log file written to ``cwd`` because a module defaulted to
      ``"."``.
    * A cache written to ``cwd`` on first run.

    All of those would be invisible to unit tests that stub the
    filesystem. The integration test is the only one that can catch
    them, because only an integration test actually *has* a
    filesystem to write to.

    The snapshot is taken *after* the subprocess run, and compared
    against a baseline taken *before* it, so the test is robust to
    whatever the OS or the test runner itself creates in ``tmp_path``
    (some systems create ``.pytest_cache``, ``__pycache__``, etc.).
    """
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Snapshot the filesystem *before* the run, then compare the
    # difference after. ``rglob("*")`` walks the whole tree under
    # ``tmp_path``, so any file the run creates *outside* of
    # ``output_dir`` would show up as a new entry that is not
    # under ``output_dir``.
    def _snapshot() -> set[Path]:
        return {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}

    before = _snapshot()

    result = run_create(
        "python.fastapi.api",
        "--name",
        "my-api",
        "--output",
        str(output_dir),
        "--no-interactive",
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"create failed unexpectedly\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    after = _snapshot()
    new_paths = after - before

    # Ignore known-good paths: the output directory itself (which
    # the run is allowed to create), and test-runner artifacts
    # that pytest produces outside the run's control.
    ignore_prefixes = {Path("out"), Path(".pytest_cache"), Path("__pycache__")}

    def _is_ignored(rel: Path) -> bool:
        return any(rel == prefix or prefix in rel.parents for prefix in ignore_prefixes)

    unexpected = {p for p in new_paths if not _is_ignored(p)}

    assert not unexpected, (
        f"create wrote files outside the designated output directory:\n"
        f"  output_dir = {output_dir}\n"
        f"  unexpected = {sorted(str(p) for p in unexpected)}\n"
        f"Files outside --output are a side-effect bug: the command must "
        f"only write to the path the user chose."
    )
