"""End-to-end integration test for the staging phase.

Scope
-----
This file is the *only* test in the staging suite that exercises the
complete pipeline through the CLI binary — ``forge create <blueprint>
--name <name> --no-interactive`` — and then inspects the resulting
filesystem state. Every other test in ``tests/unit/staging/`` calls
``StagingEngine`` (or a collaborator) directly, with fixtures,
assertions, and side effects the test author controls.

The distinction matters: unit tests catch *component* regressions
(a wrong kind dispatch, an escaping write, a leaked staging
directory), but they cannot catch a *wiring* regression — a change in
``cli/commands/create.py`` that forgets to call the engine, or one
that calls it and then throws the result away, or one that commits
the staging tree to the wrong destination. Those bugs require the
full ``forge create`` invocation to surface, and this file is where
they are caught.

Why a real subprocess, not ``typer.testing.CliRunner``
------------------------------------------------------
Two reasons:

1. **The binary is the contract.** CI runs ``forge create`` as a
   subprocess; users run ``forge create`` as a subprocess; the shell
   `--help` behaviour, the `sys.exit` codes, and the stdout/stderr
   separation are all part of the CLI's user-facing surface. A test
   that goes through ``CliRunner`` exercises the same code but
   *bypasses the process boundary* — it cannot catch a bug where the
   command works in-process but fails as a subprocess (an import
   that only resolves under the test runner, a signal handler that
   only installs in the main thread of the process, a `sys.exit`
   that the runner swallows and the shell does not).

2. **The installed-package layout is part of the contract.**
   ``python -m forge.cli`` resolves the package through the normal
   import machinery, which is what a user's installed ``forge``
   script does. Running the CLI in-process lets the test runner's
   ``sys.path`` mask a missing package-data declaration (a
   ``blueprints/`` directory that ships in the wheel but is not
   found by the installed package, or vice versa). The subprocess
   proves the *installed* package is complete.

The trade-off is speed: a subprocess costs ~200 to 500ms vs. ~10ms for
an in-process call. That is acceptable for a file with five tests
whose value is precisely the process boundary; the unit tests already
cover the logic, and this file is meant to be slow-but-thorough.

The default-vs-explicit contract
--------------------------------
The blueprint declares ``include_docker`` with ``default: true``. The
resolver supplies that default when the user does not pass
``--var include_docker=...``, so ``forge create`` **without any
override** produces the Docker files. This is the documented
behaviour — the blueprint's default *is* the plan default, not merely
a wizard-prompt default — and the file verifies it in three
complementary ways:

    * **Default path** — no ``--var`` flag: eleven files (eight
      unconditional + three conditional).
    * **Explicit enable** — ``--var include_docker=true``: eleven
      files, same as the default, but via a different code path
      (the value comes from the flag, not the default).
    * **Explicit disable** — ``--var include_docker=false``: eight
      files. This is the *negative* half of the default — it proves
      that opting out actually opts out.

Testing only the "explicit enable" path would pass if a future
blueprint change flipped the default to ``false`` (the explicit flag
would still work), while silently changing the *default* behaviour of
``forge create`` for every user who does not pass the flag. Testing
only the "default" path would pass if a future resolver change
stopped applying defaults (the condition would evaluate against an
absent value, which is not currently a valid state, but would
silently produce no files if the resolver were made to skip defaults
for optional variables). The three tests together pin the contract in
both directions.

What this file deliberately does *not* cover
--------------------------------------------
    * Behaviour under `--dry-run` — the dry-run path does not commit
      and therefore never writes to the destination. Its contract
      (plan output, exit codes) is covered in
      ``tests/integration/test_dryrun_command.py``.
    * Collision handling on an existing destination *with* the
      ``--overwrite`` flag — that is a different wiring (safety
      layer + backup behaviour), covered in
      ``tests/integration/test_safety_pipeline.py``. The
      *without* ``--overwrite`` path *is* covered here, by
      ``test_create_is_idempotent_when_destination_already_exists``,
      because it is the exact path a user who ran ``forge create``
      twice by mistake would encounter.
    * Template *rendering* correctness — the rendered content is
      asserted by the unit tests; the CLI only passes the content
      through. A wiring test does not need to re-verify what the
      engine already tests.
    * Failure-path cleanup *at the CLI level* — the unit tests
      already exercise "engine aborts on failure and cleans up";
      duplicating that through the CLI would cost a subprocess per
      case without adding coverage beyond what the failure-path
      tests provide.

The rule of thumb the codebase uses: **a test belongs in
``integration/`` if it exercises something *only* the process
boundary can break.** Here, that is the wiring between the CLI and
the engine, and the package's shipped-by-default state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run_create(
    tmp_path: Path,
    *extra_args: str,
    blueprint: str = "python.fastapi.api",
    name: str = "demo",
) -> subprocess.CompletedProcess[str]:
    """Invoke ``forge create`` in a subprocess, capturing both streams.

    The command is *non-interactive* (``--no-interactive``), so the
    test does not depend on whether the CI environment has a TTY on
    stdin. A test that accidentally ran in interactive mode would
    hang on the first prompt — subprocess timeouts would catch that
    eventually, but ``--no-interactive`` makes the failure impossible
    to introduce by mistake.

    ``cwd=tmp_path`` because the CLI's default ``--output`` is
    ``Path(".")`` — the current working directory. Running the
    subprocess inside ``tmp_path`` means the project is created at
    ``tmp_path / <name>``, which is what the assertions expect. A
    test that passed `--output` explicitly would work too, but it
    would not exercise the default-output path, which is the path
    users take most often.

    ``text=True`` because the assertions in this file are about
    *human-readable* output and exit codes, not raw bytes. A future
    test that needed to assert on encoded output (a ``--json`` mode
    assertion) would use ``capture_output=True`` without ``text``;
    this file's five tests do not need that.

    The return type is annotated as
    ``subprocess.CompletedProcess[str]`` rather than
    ``subprocess.CompletedProcess`` because mypy strict treats the
    unparameterised form as ``CompletedProcess[Any]`` and cannot
    verify that ``proc.stdout`` is a string. The parameterised form
    gives mypy enough information to check the assertions, and gives
    a reader the return shape without having to look up the type.
    """
    return subprocess.run(
        [  # noqa: S603
            sys.executable,
            "-m",
            "forge.cli",
            "create",
            blueprint,
            "--name",
            name,
            "--no-interactive",
            *extra_args,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _clean_env() -> dict[str, str]:
    """Return a copy of the environment with test-hostile variables cleared.

    Two namespaces are cleared, for two different reasons:

    * **``FORGE_*``** — the CLI reads ``FORGE_NO_COLOR``,
      ``FORGE_DEBUG``, and similar variables (see
      ``forge.cli.ui._is_colour_enabled``). A developer running the
      test suite with one of those set in their shell would produce a
      different result than CI — a "works on my machine" failure that
      is entirely avoidable. Clearing by prefix (not by enumerating
      the specific variables the current code reads) means a new
      ``FORGE_*`` variable added in a future release is automatically
      neutralised without requiring an update here.

    * **``COV_CORE_*``** — ``pytest-cov`` sets these for the parent
      process when ``--cov`` is active. A child subprocess inherits
      them via ``os.environ`` and, if it has ``coverage`` installed,
      will write its own ``.coverage.*`` file. That is not the child's
      job — the child is not under test for coverage — and it makes
      the parent's ``pytest-cov`` combine step flakier. Worse, when
      the parent runs *without* ``--cov`` (e.g. for a focused
      debugging run), an inherited ``COV_CORE_*`` from a shell
      wrapper would still cause the child to start writing coverage
      data, silently. Clearing them here makes the test file
      coverage-agnostic: the subprocess behaves the same whether the
      parent runs with or without coverage, which is what the
      integration test is supposed to observe.

    The function returns a *copy* so the test does not mutate the
    process's environment (which would leak into other tests in the
    same pytest session, including tests that *want* a specific
    environment variable set). The pattern — "read, filter, return
    copy" — is the only safe way to override environment for a
    subprocess without an ``env=`` merge.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("FORGE_") and not k.startswith("COV_CORE_")
    }


def _assert_create_succeeded(proc: subprocess.CompletedProcess[str]) -> None:
    """Assert the subprocess exited 0, with both streams in the report.

    Both streams are included in the assertion message because a
    failure in the create path can land on either stream: stdout
    carries the progress lines and success message; stderr carries
    any warning the engine emitted (and, if a `_progress` bug let
    progress leak to stderr in a --json mode, that would show up
    here too). A failure report with only the exit code would leave
    the reader to re-run the test to see what the CLI actually said.

    The check is ``returncode == 0`` rather than ``not returncode``
    because the exit code contract (ADR-0013) reserves 0 for
    success and specific non-zero values for specific failure modes.
    ``not returncode`` would pass for a hypothetical ``None`` return
    code, which would be a bug in ``subprocess.run`` (not a thing,
    but the explicit check documents the intent better than the
    falsy shortcut).
    """
    assert proc.returncode == 0, (
        f"forge create failed with exit code {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


def _walk_project_files(project: Path) -> set[str]:
    """Return every regular file under ``project`` as a POSIX-style relpath.

    ``POSIX-style`` means forward slashes on every platform, so the
    expected-file sets in this file (which are written with forward
    slashes) compare correctly on Windows. ``os.walk`` yields paths
    with the OS separator (``\\`` on Windows); ``Path.as_posix()``
    converts.

    Directories are excluded (``filenames`` already does this at each
    step of the walk). Empty directories are not visible in the
    returned set — see the "no more" assertion's comment for why
    that is the correct level of strictness.
    """
    found: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(project):
        for name in filenames:
            relpath = (Path(dirpath) / name).relative_to(project).as_posix()
            found.add(relpath)
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_create_writes_a_complete_project_to_the_destination(tmp_path: Path) -> None:
    """A full ``forge create`` (no overrides) populates the destination.

    The end-to-end assertion for the *default* invocation: given a
    known blueprint and no ``--var`` overrides, the destination
    contains exactly the files the plan promised — no more, no less —
    and nothing from the staging machinery is visible.

    Why this test's expected set includes the Docker files
    ------------------------------------------------------
    The blueprint declares ``include_docker`` with ``default: true``,
    and the resolver applies that default when the user does not pass
    ``--var include_docker=...``. So ``forge create`` **without any
    override** produces a project *with* the Docker files. This is
    the documented behaviour (see ``create.py``'s ``--var`` help
    text: "Overrides the blueprint default"), and the test asserts it
    explicitly.

    Testing only the "no override" case, or only the "explicit
    override" case, would miss half of the contract. See the module
    docstring's "The default-vs-explicit contract" section for the
    three-case matrix this file covers.

    "No more, no less" is the union of three properties, each of which
    a wiring regression could break independently:

        * **No less** — every planned file is present. A bug that
          silently skipped a file (say, an off-by-one in
          ``_commit_staged``'s manifest iteration, or a render that
          failed without aborting) would fail this.
        * **No more** — no unplanned files appear. A bug that wrote
          a marker file, a lock file, or a summary at the destination
          root would fail this. The check is on the *destination's*
          contents, not on a specific set — any extra file is a
          failure.
        * **No debris** — no ``.forge-stage-*`` directory remains.
          A bug in ``StagingResult.destroy()`` or in the wiring that
          forgot to call it would leave the staging tree in place.
          The check is on the destination's parent, because that is
          where the staging tree is a sibling.

    The three are separated into distinct assertions so a failure
    points at the property that broke. A combined assertion (`assert
    set(found) == expected and no debris`) would produce a failure
    message that named a diff without saying which property failed;
    three assertions produce three precise failure messages.

    The specific files checked here are derived from the blueprint's
    ``files[]`` *and* ``conditional_files[]`` entries, with the
    conditional group included because the default value for its
    condition is ``true``. Excluding the conditional files here would
    make the set a "default invocation minus whatever the conditional
    files happen to be" set, which is not a well-defined contract —
    it would drift the moment the default changed.

    Hard-coding the expected set is deliberate:

    * The test's purpose is to prove *the wiring produces the right
      output*, and a dynamically-derived expected set (from the
      blueprint's own plan) would pass for a wiring bug that used
      the wrong blueprint — the expected set and the actual output
      would drift together.
    * A hard-coded set is more brittle to intentional fixture changes
      (the set must be updated alongside the blueprint, which is the
      correct coupling) and more sensitive to unintentional ones
      (a blueprint that silently stops producing a file is caught
      immediately) — which is the trade we want for a test whose
      job is "the pipeline does what we think it does."

    The set is **complete**: every file the default invocation
    produces is asserted. A subset would silently weaken the "no
    less" half — the "no more" assertion would still catch spurious
    files, but a dropped file would pass unnoticed. This is the
    reason the set is longer than the three or so files a typical
    smoke test would check.
    """
    proc = _run_create(tmp_path)
    _assert_create_succeeded(proc)

    project = tmp_path / "demo"

    # (1) No less — every planned file is present.
    #
    # Source: the `files:` *and* `conditional_files:` entries in
    # `src/forge/blueprints/python.fastapi.api/blueprint.yaml`. The
    # conditional group is included because `include_docker` defaults
    # to `true`; see the docstring above for the reasoning.
    expected_files = {
        # Unconditional files — always present.
        "pyproject.toml",
        "README.md",
        ".gitignore",
        "src/demo/__init__.py",
        "src/demo/main.py",
        "src/demo/settings.py",
        "tests/conftest.py",
        "tests/test_health.py",
        # Conditional files — present because `include_docker` defaults
        # to `true`, and the resolver supplies that default when no
        # `--var include_docker=...` override is passed.
        "Dockerfile",
        ".dockerignore",
        "compose.yaml",
    }
    for relpath in sorted(expected_files):
        assert (
            project / relpath
        ).is_file(), f"planned file {relpath!r} is missing from the created project"

    # (2) No more — the project contains exactly the planned files.
    #
    # Walk the project tree and collect every regular file (relative
    # to ``project``). Directories are not asserted on directly: the
    # set of files implies the set of directories, and asserting on
    # directories separately would make the test brittle to harmless
    # empty-directory changes (say, if a blueprint started shipping
    # an empty ``tests/`` placeholder).
    found_files = _walk_project_files(project)

    assert found_files == expected_files, (
        "the created project does not contain exactly the planned files.\n"
        f"missing: {sorted(expected_files - found_files)!r}\n"
        f"unexpected: {sorted(found_files - expected_files)!r}"
    )

    # (3) No debris — no staging tree remains anywhere under tmp_path.
    #
    # The glob is on ``tmp_path`` (the destination's parent), not on
    # ``tmp_path / "demo"`` — the staging tree was a sibling of the
    # destination, so it lives in ``tmp_path``, not inside the
    # project. A test that only checked inside the project would pass
    # for a bug that failed to destroy the staging tree.
    leftover = list(tmp_path.glob(".forge-stage-*"))
    assert leftover == [], (
        "the CLI left staging debris after a successful run: " f"{[p.name for p in leftover]!r}"
    )


def test_create_with_docker_includes_conditional_files(tmp_path: Path) -> None:
    """A run with ``--var include_docker=true`` explicitly includes the Docker files.

    The three files under ``conditional_files:`` in the blueprint
    (``Dockerfile``, ``.dockerignore``, ``compose.yaml``) are produced
    when ``include_docker == true``. They are *also* produced by the
    default path (see the previous test), because the blueprint
    declares ``include_docker`` with ``default: true``. This test
    asserts the same *outcome* via a *different code path*: the flag
    is supplied explicitly, so the value comes from the CLI's
    ``--var`` handling rather than from the resolver's default
    application.

    Why test the same outcome twice (via default and via explicit
    flag): the two paths share most of the resolution pipeline but
    differ at the boundary between ``--var`` parsing and the
    resolver's precedence rules. A regression in either path would
    pass the *other* path and be caught only by the test that
    exercises it. Two tests, two paths, one shared contract — the
    exact redundancy the codebase uses elsewhere (see the "one
    concern, multiple independent checks" pattern in the safety
    tests).

    Why the assertion is a subset of the previous test's file set:
    this test's job is the *explicit-enable* path, not the
    full-project assertion. The previous test asserts the full set;
    re-asserting it here would duplicate that coverage without adding
    diagnostic value — a failure on this test should point at the
    Docker files specifically, not at ``pyproject.toml``.

    Why ``--var include_docker=true`` and not something like
    ``--include-docker``: the CLI's surface for blueprint variables
    is ``--var key=value`` (per ``cli/commands/create.py``'s
    ``--var`` option). A dedicated flag per variable would be a
    different design; the ``--var`` form is what the CLI contract
    offers, and this test exercises it.
    """
    proc = _run_create(tmp_path, "--var", "include_docker=true")
    _assert_create_succeeded(proc)

    project = tmp_path / "demo"
    for relpath in ("Dockerfile", ".dockerignore", "compose.yaml"):
        assert (project / relpath).is_file(), (
            f"conditional Docker file {relpath!r} is missing — the "
            "explicit-enable path may be broken"
        )


def test_create_without_docker_excludes_conditional_files(tmp_path: Path) -> None:
    """A run with ``--var include_docker=false`` omits the Docker files.

    The **negative half** of the default-vs-explicit contract. The
    previous two tests verify that the Docker files appear (via the
    default and via an explicit enable). This one verifies they
    *don't* appear when the user explicitly opts out.

    Without this test, the "default" and "explicit enable" tests
    would both pass even if ``--var include_docker=false`` were
    silently ignored — the flag would be accepted without effect,
    and the user's stated preference would be discarded. That is the
    exact class of silent failure the "fail loudly" principle in
    ADR-0019 forbids, and it is what this test exists to catch.

    The test also asserts the *unconditional* files are still
    present. A bug that mis-wired the conditional evaluation (say,
    by short-circuiting the entire plan when any condition evaluated
    false) would drop the Docker files — passing the "excludes"
    assertion — while also silently dropping every other file. The
    unconditional-files assertion catches that: the plan must produce
    its unconditional subset regardless of any conditional group's
    verdict.
    """
    proc = _run_create(tmp_path, "--var", "include_docker=false")
    _assert_create_succeeded(proc)

    project = tmp_path / "demo"

    # The Docker files must be absent.
    for relpath in ("Dockerfile", ".dockerignore", "compose.yaml"):
        assert not (project / relpath).exists(), (
            f"conditional Docker file {relpath!r} was produced despite "
            "--var include_docker=false — the explicit-disable path "
            "may be broken"
        )

    # The unconditional files must still be present. Without this
    # assertion, a bug that dropped every file when a condition
    # evaluated false would pass the "excludes" checks above.
    for relpath in (
        "pyproject.toml",
        "README.md",
        ".gitignore",
        "src/demo/__init__.py",
        "src/demo/main.py",
        "src/demo/settings.py",
        "tests/conftest.py",
        "tests/test_health.py",
    ):
        assert (project / relpath).is_file(), (
            f"unconditional file {relpath!r} is missing when "
            "--var include_docker=false — the conditional evaluation "
            "may have aborted the whole plan"
        )


def test_create_output_reports_success_without_internal_noise(tmp_path: Path) -> None:
    """The user-facing output names the project and stays free of internals.

    Two things the CLI's output must do:

        * **Report success.** A successful `forge create` prints a
          line that names the project (the user should not have to
          check the filesystem to know the run succeeded) and, when
          run with a TTY, uses the CLI's standard success frame
          (``✓ ...`` — see ``cli/ui.py``).

        * **Stay free of internals.** No traceback, no stack frame,
          no Python module path, no staging directory path should
          appear in the output on success. The user asked for a
          project; the CLI gave them one; showing them the machinery
          that produced it is noise, and — worse — a class of
          information leak (a staging directory path could reveal
          the user's home directory in a report the user shares).

    The assertion is on the *substring* ``demo`` in stdout rather
    than the exact success message. The exact wording may evolve
    (it is deliberately not part of the contract; see the CLI's
    ``--help`` docstrings), and pinning it here would make every
    wording change a test change for no diagnostic benefit. The
    substring check is the property that matters: "the user sees
    their project name in the success line."

    The "no internals" check is on *both* streams. A future
    regression that moved progress output to stderr (perhaps to keep
    stdout clean for a future `--json`) would still be free of
    tracebacks, but the assertion is on the *absence* of the specific
    internal markers (`Traceback`, `File "`, `.forge-stage-`), not on
    the presence of user-facing text. That is the right shape for a
    negative assertion: it names the specific things that must not
    appear, rather than trying to enumerate everything that must.
    """
    proc = _run_create(tmp_path)
    _assert_create_succeeded(proc)

    # (1) Success is reported, and names the project.
    assert "demo" in proc.stdout, (
        f"success output does not name the project (``demo``)\n" f"stdout:\n{proc.stdout}"
    )

    # (2) No internal noise on either stream.
    combined = proc.stdout + proc.stderr
    for forbidden in ("Traceback", 'File "', ".forge-stage-"):
        assert forbidden not in combined, (
            f"internal marker {forbidden!r} leaked into user output\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


def test_create_is_idempotent_when_destination_already_exists(tmp_path: Path) -> None:
    """A second run against the same ``--output`` fails cleanly, not silently.

    The staging engine and the safety layer both handle "destination
    already exists" — the safety layer refuses the collision (exit 6)
    unless ``--overwrite`` is passed. That is the *intended* behaviour,
    but it is only observable end-to-end: a wiring regression that
    forgot to run the safety check (or that caught its exception
    and continued) would let the second run overwrite the first
    project's files silently, which is the exact class of "silent
    data loss" the safety layer exists to prevent.

    The test:

        1. Runs ``forge create`` once — succeeds, writes the project.
        2. Runs ``forge create`` again against the *same* ``tmp_path``
           with the same name — should fail with exit 6 (COLLISION)
           and leave the first project untouched.
        3. Verifies the first project's files were not modified
           (checked by hashing one representative file before and
           after the second run).

    Why hash a representative file instead of every file: the
    staging engine's per-file atomicity and the safety layer's
    collision detection both guarantee that *no* file is touched on
    a collision (the safety layer fires before any write). Hashing
    every file would test that guarantee redundantly; hashing one
    file catches the *regression* (a write happened before the
    collision was detected), which is the property this test cares
    about. A future need to prove every file untouched would call
    ``forge.dryrun.snapshot.fingerprint``, but that is overkill for
    a wiring test whose purpose is "the safety layer runs before any
    write."

    The collision exit code (6) is asserted explicitly rather than
    "non-zero." The exit code is a documented contract (ADR-0013);
    a wiring regression that changed it (or that caught the
    ``CollisionError`` and exited 1) would pass a "non-zero" check
    but break CI scripts that distinguish "collision, retry with
    --overwrite" from "user error, fix the flags." The specific
    code is what makes that distinction enforceable.
    """
    import hashlib

    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    # (1) First run — succeeds.
    first = _run_create(tmp_path)
    _assert_create_succeeded(first)

    pyproject = tmp_path / "demo" / "pyproject.toml"
    assert pyproject.is_file(), (
        "first run did not produce pyproject.toml — the precondition "
        "for the idempotency check is broken"
    )
    before_hash = _sha(pyproject)

    # (2) Second run against the same destination — fails with exit 6.
    second = _run_create(tmp_path)
    assert second.returncode == 6, (
        "a second `forge create` against the same destination must "
        "fail with exit code 6 (COLLISION). This is the contract CI "
        "scripts rely on to distinguish 'retry with --overwrite' from "
        "'fix the flags'. Got exit code "
        f"{second.returncode}\n"
        f"stdout:\n{second.stdout}\n"
        f"stderr:\n{second.stderr}"
    )

    # (3) The first run's files were not touched.
    after_hash = _sha(pyproject)
    assert before_hash == after_hash, (
        "the second `forge create` modified the destination before "
        "the collision was detected — the safety layer must refuse "
        "before any write"
    )
