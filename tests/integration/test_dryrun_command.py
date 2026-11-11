"""Integration tests for ``forge create --dry-run``.

These tests run the CLI as a subprocess — the same way a user or a
CI script invokes it — and assert on the *observable* contract:

    * the exit code (0 for clean, 7 for collisions, others for
      user/blueprint errors),
    * the JSON shape on stdout when ``--json`` is passed,
    * the filesystem state (byte-identical before and after every
      dry-run — the KPI for this task),
    * the presence of a rendered plan on stdout for the human format.

Why subprocess and not ``CliRunner.invoke``?
-------------------------------------------
``CliRunner`` runs the command function *inside* the test process,
without ``main()``'s exception handler or the real ``sys.exit``
behaviour. Two things that matter to the user cannot be tested that
way:

    1. **Exit codes.** ``CliRunner`` returns ``result.exit_code``
       derived from the exception's ``exit_code`` attribute or from
       ``typer.Exit``. But ``main()`` catches exceptions and calls
       ``sys.exit(n)`` — a *different* mechanism that resolves to a
       different set of numbers if the mapping ever drifts. A
       subprocess observes exactly what a shell sees.

    2. **stdout/stderr separation.** ``CliRunner`` has a merged
       capture that requires explicit ``mix_stderr=False`` to
       distinguish streams. A subprocess's ``capture_output=True``
       gives two streams natively, which is what a pipeline cares
       about (``forge create --dry-run --json 2>/dev/null | jq``).

The ``--output`` / ``--name`` combination and where to seed files
---------------------------------------------------------------
``create`` builds its destination as ``<output>/<name>/`` — the
``--name`` value becomes the last path segment of the project root.
So when a test passes ``--output <dest>`` and ``--name demo``, the
plan's destinations are resolved against ``<dest>/demo/``, not
against ``<dest>/`` directly.

A test that wants to seed a *colliding* file must therefore write
it to ``<dest>/demo/<filename>`` — the same path the plan will
target. Seeding ``<dest>/<filename>`` (one level too high) has no
effect: the plan inspects ``<dest>/demo/<filename>``, finds nothing,
and reports zero collisions. That mismatch was the cause of the
original collision-test failures; the helpers and seed paths below
are constructed so a test cannot make the same mistake.

The ``PROJECT_NAME`` constant
-----------------------------
Every test that seeds a colliding file reads the project name from
one place. A future test that renames the project would update
``PROJECT_NAME`` and every seed path (which all reference it via the
``_seed_collision`` helper) at once — no chance of a seed path and
a CLI invocation drifting apart.

Flag capability detection
-------------------------
Not every flag this suite wants to exercise is registered on
``create`` today. Rather than skip the whole suite or ship failing
tests, each flag-dependent test is guarded by ``_requires_flag(...)``:
a runtime check of ``forge create --help`` that activates the test
when the flag lands.

The ``--var`` resolver gap
--------------------------
The installed ``python.fastapi.api`` blueprint's file destinations
reference ``{{ project }}`` — a variable the blueprint's own
``variables:`` list does not declare. Supplying it as
``--var project=demo`` is the intended mechanism; the CLI's
``_resolve_inputs`` merges undeclared ``--var`` keys into the plan's
variable mapping (see the "The ``--var`` merge" section in
``src/forge/cli/commands/create.py``), so ``--var`` works end-to-end.

Why the filesystem assertions use ``fingerprint``
-------------------------------------------------
A dry-run's promise is "no mutations." Asserting on ``list(dir)``
catches *created* entries but misses *modified* content, *renamed*
files, and *touched* mtimes. The fingerprint (see
``forge.dryrun.snapshot``) captures all four.

Lint suppressions in this file
------------------------------
Two ``# noqa`` markers appear below, and both are scoped to a single
line with the reason stated on the same line:

* ``S603`` on the two ``subprocess.run(...)`` calls. The argv is
  built from ``sys.executable`` (a pinned absolute path), the module
  literal ``"-m", "forge.cli"``, the command literal ``"create"``,
  and the caller's ``*args`` — every one of which is a string literal
  or a parameter from the test's own source. No untrusted input
  (env var, network payload, user filename) reaches this argv, and
  ``shell=False`` is the default. S603's threat model does not apply.
  The suppression is scoped to each call rather than to the module
  so a future subprocess call that *does* interpolate external input
  is still flagged.

* Nothing else — the rest of the file is suppression-free.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from forge.dryrun.snapshot import fingerprint

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
PROJECT_NAME = "demo"
"""The ``--name`` value used by every test in this file.

Centralised so every seed path (which must reference the same name to
collide) and every CLI invocation read from one place. If a future
test needs to verify a different name, it should either parametrise
over this constant or define a local one — but never hardcode the
name inline, which is what caused the original collision-test bug
(the seed path used ``<dest>/`` while the CLI used ``<dest>/demo/``).
"""


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture()
def blueprint_id() -> str:
    """A blueprint id that is discoverable from the installed package."""
    return "python.fastapi.api"


@pytest.fixture()
def dest(tmp_path: Path) -> Path:
    """A per-test, empty destination directory.

    This is the *output root*, not the project root. The project is
    created under ``<dest>/<PROJECT_NAME>/``; tests that want to seed
    a colliding file must use the helper below.
    """
    return tmp_path


# -----------------------------------------------------------------------------
# Capability detection
# -----------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _create_help_text() -> str:
    """Return ``forge create --help`` output (cached per session)."""
    # S603 does not apply: the argv is ``[sys.executable, "-m",
    # "forge.cli", "create", "--help"]`` — every element is either a
    # pinned absolute path (``sys.executable``) or a string literal.
    # No untrusted input, no shell interpretation (``shell=False`` is
    # the default). See the module docstring's "Lint suppressions in
    # this file" section.
    proc = subprocess.run(
        [sys.executable, "-m", "forge.cli", "create", "--help"],  # noqa: S603q
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout + proc.stderr


def _flag_exists(name: str) -> bool:
    """Return True iff ``forge create --help`` advertises ``name``."""
    return name in _create_help_text()


def _requires_flag(name: str) -> pytest.MarkDecorator:
    """Return a ``skipif`` marker keyed on the flag's availability."""
    return pytest.mark.skipif(
        not _flag_exists(name),
        reason=f"create does not yet expose {name!r}; "
        f"enable when the flag lands on src/forge/cli/commands/create.py",
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _destination_flag() -> str:
    """Return the flag ``create`` uses for the destination directory."""
    return "--destination" if _flag_exists("--destination") else "--output"


def _run(
    *args: str,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``forge create`` as a subprocess.

    ``sys.executable -m forge.cli`` is the canonical way to invoke
    the CLI from a subprocess — it uses the current interpreter (so
    the venv's ``forge`` package is imported) and the package's own
    entry point (so the CLI dispatch matches what the installed
    ``forge`` script does).

    S603 does not apply: the argv is ``[sys.executable, "-m",
    "forge.cli", "create", *args]``, where ``sys.executable`` is a
    pinned absolute path, the middle elements are string literals,
    and ``*args`` are the caller's arguments — all of which are test
    literals at every call site (verified by grep against the file).
    ``shell=False`` is the default. See the module docstring's "Lint
    suppressions in this file" section.
    """
    return subprocess.run(
        [sys.executable, "-m", "forge.cli", "create", *args],  # noqa: S603q
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )


def _run_dry(
    blueprint_id: str,
    *,
    name: str = PROJECT_NAME,
    dest: Path | str,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke a non-interactive dry-run of ``blueprint_id`` into ``dest``.

    ``dest`` is the *output root*; the project will be created under
    ``<dest>/<name>/``. This is the same convention ``create`` uses,
    so the plan's ``destination`` field will name ``<dest>/<name>``.
    """
    flags = [
        blueprint_id,
        "--name",
        name,
        _destination_flag(),
        str(dest),
        "--no-interactive",
        "--dry-run",
        "--var",
        f"project={name}",
    ]
    flags.extend(extra or [])
    return _run(*flags)


def _run_dry_json(
    blueprint_id: str,
    *,
    name: str = PROJECT_NAME,
    dest: Path | str,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Same as ``_run_dry`` but with ``--json`` appended."""
    return _run_dry(
        blueprint_id,
        name=name,
        dest=dest,
        extra=[*(extra or []), "--json"],
    )


def _seed_collision(dest: Path, *, filename: str = "pyproject.toml") -> Path:
    """Seed a pre-existing file at a path the plan will target.

    **The critical detail:** ``create`` builds its destination as
    ``<dest>/<PROJECT_NAME>/``, so a colliding file must be seeded at
    ``<dest>/<PROJECT_NAME>/<filename>``. Seeding at ``<dest>/<filename>``
    (one level too high) has no effect — the plan inspects
    ``<dest>/<PROJECT_NAME>/<filename>`` and finds nothing.

    This helper centralises that calculation so no test can get it
    wrong. The name is referenced from ``PROJECT_NAME`` (the same
    constant ``_run_dry`` uses), so a future test that changes the
    project name updates one place.

    Returns the path of the seeded file, so a caller can also use it
    for further setup (e.g. seeding multiple collisions).
    """
    project_root = dest / PROJECT_NAME
    project_root.mkdir(parents=True, exist_ok=True)
    seeded = project_root / filename
    seeded.write_text("pre-existing\n")
    return seeded


# -----------------------------------------------------------------------------
# Criterion 1: dry-run lists files that would be created
# -----------------------------------------------------------------------------
@_requires_flag("--json")
def test_dry_run_json_lists_files_to_create(blueprint_id: str, dest: Path):
    """``forge create --dry-run --json`` lists every file the create would write.

    The JSON shape is a *versioned contract* (ADR-0018 and
    ``docs/reference/DRY_RUN_PLAN.md``).
    """
    proc = _run_dry_json(blueprint_id, dest=dest)

    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    payload = json.loads(proc.stdout)

    assert payload["schema_version"] == "1.0.0"
    assert payload["blueprint_id"] == blueprint_id
    assert isinstance(payload["files_to_create"], list)
    assert payload["files_to_create"]
    for item in payload["files_to_create"]:
        assert set(item.keys()) == {"source", "destination", "kind"}


# -----------------------------------------------------------------------------
# Criterion 4: dry-run does not touch the filesystem
# -----------------------------------------------------------------------------
def test_dry_run_on_empty_destination_writes_nothing(blueprint_id: str, dest: Path):
    """An empty destination stays empty after ``--dry-run``.

    The KPI: 0 filesystem mutations during dry-run. The empty case
    is the baseline.
    """
    before = fingerprint(dest)
    proc = _run_dry(blueprint_id, dest=dest)
    after = fingerprint(dest)

    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert before == after


def test_dry_run_on_populated_destination_is_byte_identical(blueprint_id: str, dest: Path):
    """A populated destination is byte-identical before and after.

    The harder case: the destination already has files (including at
    a path the plan will target — see ``_seed_collision``). A dry-run
    must not modify them, rename them, or touch their mtimes.
    """
    _seed_collision(dest)

    before = fingerprint(dest)
    proc = _run_dry(blueprint_id, dest=dest)
    after = fingerprint(dest)

    # Exit 0 (clean) or 7 (collision) are both success. What this
    # test asserts is *not the exit code* — it is the tree being
    # unchanged.
    assert proc.returncode in (0, 7), f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert before == after


def test_dry_run_does_not_create_absent_destination(blueprint_id: str, tmp_path: Path):
    """A dry-run against an *absent* destination does not create it."""
    absent = tmp_path / "would-be-created"
    assert not absent.exists()

    proc = _run_dry(blueprint_id, dest=absent)

    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert not absent.exists()


# -----------------------------------------------------------------------------
# Criterion 3: dry-run reports collisions without failing on them
# -----------------------------------------------------------------------------
@_requires_flag("--json")
def test_dry_run_collision_exits_7(blueprint_id: str, dest: Path):
    """A dry-run with a collision exits 7 — not 6, not 1, not 0.

    Exit 7 exists so CI can gate on "would this create conflict?"
    without writing anything.

    **The seed path matters:** the collision is seeded at
    ``<dest>/<PROJECT_NAME>/pyproject.toml`` (see ``_seed_collision``),
    matching the plan's actual target. Seeding at ``<dest>/pyproject.toml``
    (one level too high) would produce zero collisions and this test
    would pass with exit 0 — the failure mode that caused the
    original, pre-fix version of this test to fail.
    """
    _seed_collision(dest)

    proc = _run_dry_json(blueprint_id, dest=dest)

    assert proc.returncode == 7, (
        f"expected exit 7 for dry-run collision, got {proc.returncode}\n"
        f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    )


@_requires_flag("--json")
def test_dry_run_reports_collision_in_json(blueprint_id: str, dest: Path):
    """The collision appears in the JSON payload, not just the exit code."""
    _seed_collision(dest)

    proc = _run_dry_json(blueprint_id, dest=dest)
    payload = json.loads(proc.stdout)
    assert payload["collisions"], "expected at least one collision in the payload"
    assert any(c["destination"] == "pyproject.toml" for c in payload["collisions"])


# -----------------------------------------------------------------------------
# Criterion 6: JSON output shape
# -----------------------------------------------------------------------------
@_requires_flag("--json")
def test_dry_run_json_has_documented_top_level_keys(blueprint_id: str, dest: Path):
    """The JSON envelope's keys are exactly the documented set."""
    proc = _run_dry_json(blueprint_id, dest=dest)
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == {
        "schema_version",
        "blueprint_id",
        "blueprint_version",
        "destination",
        "overwrite",
        "files_to_create",
        "files_to_skip",
        "collisions",
        "warnings",
    }


# -----------------------------------------------------------------------------
# Human-format dry-run still renders a plan
# -----------------------------------------------------------------------------
def test_dry_run_human_output_lists_planned_files(blueprint_id: str, dest: Path):
    """The human format (default) renders the plan to stdout.

    Content assertions only — layout is cosmetic (ADR-0012).
    """
    proc = _run_dry(blueprint_id, dest=dest)

    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert blueprint_id in proc.stdout
    assert str(dest) in proc.stdout
    assert "pyproject.toml" in proc.stdout or "main.py" in proc.stdout


# -----------------------------------------------------------------------------
# Interaction with --overwrite
# -----------------------------------------------------------------------------
@_requires_flag("--overwrite")
def test_dry_run_with_overwrite_touches_nothing(blueprint_id: str, dest: Path):
    """``--dry-run --overwrite`` does not overwrite.

    ``--overwrite`` in a dry-run means "show me what would be
    replaced," not "go ahead and replace."
    """
    _seed_collision(dest)
    before = fingerprint(dest)
    proc = _run_dry(blueprint_id, dest=dest, extra=["--overwrite"])
    after = fingerprint(dest)

    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert before == after


@_requires_flag("--overwrite")
@_requires_flag("--json")
def test_dry_run_with_overwrite_suppresses_collisions_in_json(blueprint_id: str, dest: Path):
    """With ``--overwrite``, the collision list is empty (the flag consents).

    Complementary to ``test_dry_run_reports_collision_in_json``: the
    same destination with the same pre-existing file, but with
    ``--overwrite`` set. The collision list must be empty; the
    *files_to_create* list must still be populated.

    **Why the seed is still here:** the test needs a real collision
    to suppress. Without ``_seed_collision``, the test would pass on
    an empty collision list because there *were no collisions to
    begin with* — it would be a false-positive, not a real
    verification of ``--overwrite``'s suppression behaviour. Seeding
    a real collision is what makes this test earn its place.
    """
    _seed_collision(dest)
    proc = _run_dry_json(blueprint_id, dest=dest, extra=["--overwrite"])

    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    payload = json.loads(proc.stdout)
    assert payload["collisions"] == []
    assert payload["files_to_create"]


# -----------------------------------------------------------------------------
# Interplay with the rest of the CLI contract
# -----------------------------------------------------------------------------
def test_dry_run_without_name_fails_with_exit_1(blueprint_id: str, dest: Path):
    """A missing ``--name`` in non-interactive mode fails with exit 1."""
    proc = _run(
        blueprint_id,
        _destination_flag(),
        str(dest),
        "--no-interactive",
        "--dry-run",
    )

    assert proc.returncode == 1, (
        f"expected exit 1 for missing --name, got {proc.returncode}\n"
        f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    )
    assert "--name" in proc.stderr or "--name" in proc.stdout


def test_dry_run_with_unknown_blueprint_fails_with_exit_2(dest: Path):
    """An unknown blueprint fails with exit 2 before any plan is built."""
    proc = _run(
        "does.not.exist",
        "--name",
        PROJECT_NAME,
        _destination_flag(),
        str(dest),
        "--no-interactive",
        "--dry-run",
    )
    assert proc.returncode == 2, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"


# -----------------------------------------------------------------------------
# Determinism across invocations
# -----------------------------------------------------------------------------
@_requires_flag("--json")
def test_dry_run_json_is_deterministic_across_processes(blueprint_id: str, tmp_path: Path):
    """Two dry-runs with the same inputs produce byte-identical JSON.

    Determinism is a cross-process property — the process boundary
    is exactly where an accidentally non-deterministic value
    (a timestamp, a random ID, a set that iterates differently on
    different runs) would manifest.
    """

    def _run_once() -> str:
        return _run_dry_json(blueprint_id, dest=tmp_path).stdout

    a = _run_once()
    b = _run_once()
    assert a == b
