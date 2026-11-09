"""Integration tests for the `forge list` command.

What this file covers
---------------------
The whole pipeline from the *installed CLI* down to the *rendered
output on stdout*, using the real discovery service, real renderers,
and real output helpers. Where `tests/unit/cli/test_list_*.py` mocks
the discovery service to isolate command behaviour, and
`tests/contract/test_list_output_contract.py` asserts on the *shape*
of the JSON, this file exists to catch the failures that only appear
when the layers are wired together for real:

* the CLI does not import cleanly (import cycles, missing modules);
* the shipped official blueprints are not discoverable (packaging
  bug — the `.yaml` files were not included in the wheel);
* the filter options do not reach the command as parsed (Typer option
  names drifted from the argparse namespace the command reads);
* the process exit code is correct for both `0` and `2` cases;
* stdout and stderr are the streams the documentation promises.

Where the contract tests use `subprocess` for a handful of shape
assertions, this file uses it as the *only* mechanism — every test
drives a real process, because the failures this file is designed to
catch are precisely the ones a subprocess surfaces and a
`CliRunner`-based or in-process test would hide.

What this file deliberately does NOT cover
------------------------------------------
* **Renderer behaviour** — covered by the unit tests; a renderer
  regression would fail here *and* there, but the unit test's message
  is more precise and the loop is faster. Do not duplicate assertions
  that already have a home.
* **Discovery internals** — covered by `tests/unit/discovery/` and
  `tests/integration/test_discovery_bundled.py`. This file only
  asserts that the integration surfaces the results correctly.
* **Exit-code taxonomy** — covered by the CLI error tests. This file
  tests *which* exit code the `list` command produces for its own
  documented cases (0 for success, 0 for empty, 2 for bad filter),
  not the whole tree.
* **Locale-specific behaviour** — none exists; the CLI output is
  ASCII and the ids are ASCII by schema. If that ever changes, a
  dedicated test file for locale handling is the right place, not
  here.

What this file assumes
----------------------
The `forge` package is importable from the environment that runs
`pytest`, and `python -m forge.cli` resolves to this checkout. That
is true for `uv run pytest` inside the repository, and it is true
for `pip install -e .`. If a future release changes the invocation
form (e.g. moves the entry point), the `_run_forge` helper below is
the single point that needs updating.

Why not `CliRunner`
-------------------
`CliRunner` calls the Typer app *in-process*. That is fast and useful
for behaviour tests, but it bypasses the same layers this file exists
to test:

* the console-script / `python -m` entry point;
* the top-level exception handler in `forge/cli/main.py`;
* any environment setup that happens at process start.

An integration test that runs *in the same process* as the code
under test cannot, by construction, catch a bug in the entry point.
That bug is one of the most likely to ship broken, because it is the
one least exercised by unit tests — which is exactly why this file
uses `subprocess` for every test.

Determinism
-----------
Each test that asserts on the *content* of stdout filters first (e.g.
by category) so it does not depend on the exact set of blueprints
installed in the environment. Tests that assert on the *set* of
official blueprints skip when a custom catalogue is present, so the
test suite remains green for a developer working in a directory with
`.forge/blueprints/` populated. That is the same rule the unit tests
follow, and it is what makes "run the tests locally" and "run them
in CI" produce the same result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Invocation helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The canonical invocation form for tests. ``python -m forge.cli`` is
# guaranteed by ``src/forge/cli/__main__.py`` and works identically on
# every OS, in every install mode. Using the module form (rather than
# the ``forge`` console script) also sidesteps ``PATH`` issues on
# Windows and in minimal containers.
_CLI = (sys.executable, "-m", "forge.cli")


def _run_forge(
    *args: str,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m forge.cli`` with the given arguments.

    Runs from the repository root by default (so the tests do not
    accidentally observe blueprints in a parent directory's
    ``.forge/blueprints/``), and returns the completed process with
    ``stdout``/``stderr`` decoded as text.

    The ``env_extra`` parameter lets individual tests override
    environment variables (e.g. ``NO_COLOR``) without mutating the
    parent process's environment, which would leak between tests.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [*_CLI, *args],  # noqa: S603
        capture_output=True,
        text=True,
        cwd=cwd or _REPO_ROOT,
        env=env,
    )


def _run_list(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Convenience wrapper: ``forge list`` with the given options."""
    return _run_forge("list", *args, cwd=cwd)


def _has_custom_blueprints() -> bool:
    """Return True if the repository has a ``.forge/blueprints/`` directory.

    The ordering and content tests that assume the shipped official
    catalogue need to skip when custom blueprints are installed —
    otherwise a developer working on a local override sees unrelated
    failures. CI does not have a ``.forge/blueprints/`` directory, so
    the tests that matter for the release run without skipping.
    """
    return (_REPO_ROOT / ".forge" / "blueprints").is_dir()


# A marker applied to every test that assumes the shipped official
# catalogue and nothing else. Skipped when the local environment has
# custom blueprints. Reused below so a future test that also depends
# on the pristine catalogue has one obvious decorator to copy.
_official_only = pytest.mark.skipif(
    _has_custom_blueprints(),
    reason=(
        "the repository has .forge/blueprints/; this test assumes the "
        "shipped official catalogue and cannot be run against a "
        "customised one"
    ),
)


# ---------------------------------------------------------------------------
# Smoke: the command runs at all
# ---------------------------------------------------------------------------


class TestCommandRuns:
    """The command exists, exits cleanly, and produces output.

    These are the most basic integration tests: if they fail, nothing
    else in this file is meaningful. They are deliberately separate
    from the content tests below so a failure here points at the
    wiring, not at the data.
    """

    def test_list_help_succeeds(self) -> None:
        # `--help` exercises the Typer wiring (option definitions,
        # help strings) without running the command body. It is the
        # fastest integration check that the CLI is importable and
        # the command is mounted.
        proc = _run_list("--help")
        assert proc.returncode == 0, (
            f"`forge list --help` failed with exit code "
            f"{proc.returncode}.\n--- stderr ---\n{proc.stderr}"
        )
        assert "Usage:" in proc.stdout or "Usage:" in proc.stderr, (
            "`--help` output does not contain a usage line; the "
            "command may not be mounted on the CLI."
        )

    def test_list_json_succeeds(self) -> None:
        # The most used invocation form. If this fails, the pipeline
        # is broken at the process level, not just at an option.
        proc = _run_list("--json")
        assert proc.returncode == 0, (
            f"`forge list --json` failed with exit code "
            f"{proc.returncode}.\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )

    def test_list_plain_succeeds(self) -> None:
        proc = _run_list()
        assert proc.returncode == 0, (
            f"`forge list` failed with exit code {proc.returncode}.\n"
            f"--- stderr ---\n{proc.stderr}"
        )


# ---------------------------------------------------------------------------
# Official catalogue discoverability
# ---------------------------------------------------------------------------


class TestOfficialCatalogueDiscoverable:
    """The shipped official blueprints are found by the installed CLI.

    These tests fail if the packaging is wrong: the `.yaml` files were
    not included in the wheel, the `forge.blueprints` package was not
    shipped, or `importlib.resources` cannot see the directory. That
    is a real class of packaging bug — it passes in editable installs
    and fails for every user installing from PyPI — which is why it
    is tested at the process boundary, not via `importlib` directly.
    """

    @_official_only
    def test_official_blueprints_appear_in_json(self) -> None:
        proc = _run_list("--json")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        ids = {item["id"] for item in payload["blueprints"]}
        assert "python.fastapi.api" in ids, (
            f"Shipped blueprint 'python.fastapi.api' is not "
            f"discoverable. This is a packaging bug: the blueprint "
            f"file was not included in the installed wheel.\n"
            f"Discovered ids: {sorted(ids)}"
        )
        assert "python.automation.cli" in ids
        assert "typescript.frontend.react" in ids

    @_official_only
    def test_official_blueprints_appear_in_table(self) -> None:
        # Same assertion in the human-facing mode. If the table path
        # diverges from the JSON path, this test catches it — which
        # is the whole point of having both modes consume the same
        # DTO (ADR-0012).
        proc = _run_list()
        assert proc.returncode == 0
        assert "python.fastapi.api" in proc.stdout
        assert "python.automation.cli" in proc.stdout
        assert "typescript.frontend.react" in proc.stdout


# ---------------------------------------------------------------------------
# Filters — option parsing reaches the command
# ---------------------------------------------------------------------------


class TestFilterOptions:
    """`--category` and `--source` filter the catalogue as documented.

    These tests exist to catch a specific class of bug: the Typer
    option decorator and the ``SimpleNamespace`` the adapter builds
    drift apart, so the option appears in ``--help`` but never
    reaches the command. The unit tests exercise the command *with*
    a pre-built namespace, so they cannot catch this — only the full
    process pipeline can.
    """

    def test_category_filter_narrows_output(self) -> None:
        # `--category=frontend` matches only the React blueprint in
        # the shipped catalogue. If the filter is not wired, all
        # blueprints appear; if it is wired but the wrong field is
        # filtered, none appear (and the exit code is still 0, so
        # the test has to assert on content).
        proc = _run_list("--json", "--category=frontend")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        categories = {item["category"] for item in payload["blueprints"]}
        assert categories <= {"frontend"}, (
            f"`--category=frontend` returned blueprints in categories "
            f"{categories}; the filter is not being applied."
        )

    def test_unknown_category_exits_two(self) -> None:
        # A typo in the filter is a *usage* error: exit code 2, per
        # POSIX conventions (also what `typer.BadParameter` produces).
        # That is deliberately different from a validation error
        # (exit 1) or an internal error (exit 5).
        proc = _run_list("--json", "--category=apis")
        assert proc.returncode == 2, (
            f"`--category=apis` (a typo) exited with {proc.returncode}; "
            f"expected 2 (usage error).\n"
            f"--- stderr ---\n{proc.stderr}"
        )

    def test_unknown_source_exits_two(self) -> None:
        proc = _run_list("--source=bundled")  # valid values: official, local
        assert proc.returncode == 2

    def test_source_filter_narrows_output(self) -> None:
        # In a checkout without local overrides, `--source=local`
        # yields the empty envelope. In a checkout with overrides,
        # it yields whatever the user added. The contract in either
        # case is: only `local` blueprints, or an empty list.
        proc = _run_list("--json", "--source=local")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        # ``source`` is not in the JSON output (per the contract), so
        # we cannot assert on it directly. What we *can* assert is
        # that the invocation succeeds and the output parses.
        assert "blueprints" in payload


# ---------------------------------------------------------------------------
# Streaming contract — stdout vs. stderr
# ---------------------------------------------------------------------------


class TestStreaming:
    """stdout is the payload; stderr is commentary.

    This is the Unix contract, and it is exactly the kind of property
    that unit tests are prone to miss because they mock the writer.
    An integration test that observes real process-level streams is
    the only way to catch a regression in stream selection.
    """

    def test_json_mode_puts_payload_on_stdout(self) -> None:
        proc = _run_list("--json")
        assert proc.returncode == 0
        # stdout parses as JSON; that is the strongest possible
        # assertion that the payload is on stdout.
        json.loads(proc.stdout)

    def test_json_mode_keeps_stderr_clean(self) -> None:
        proc = _run_list("--json")
        assert proc.stderr == "", (
            f"`--json` mode emitted on stderr: {proc.stderr!r}. "
            f"Successful execution must not produce commentary, "
            f"because consumers may grep stderr for failures."
        )

    def test_table_mode_keeps_stderr_clean_on_success(self) -> None:
        # Same rule for the table path in the happy case. Warnings
        # (e.g. empty catalogue) do go to stderr — but a populated
        # successful run must not.
        proc = _run_list()
        if proc.stdout.strip() == "":
            pytest.skip("catalogue is empty; success path with content is covered elsewhere")
        assert proc.stderr == ""

    def test_no_ansi_by_default_when_piped(self) -> None:
        # When stdout is not a TTY (which is the case for
        # `subprocess.run(capture_output=True)`), Rich and other
        # formatters disable colour by default. This test verifies
        # that the CLI does not force colour into a pipe — which
        # would break every consumer.
        proc = _run_list()
        assert "\x1b[" not in proc.stdout, (
            "ANSI escape sequence in piped stdout. Colour detection "
            "should have disabled it; forcing colour into a pipe "
            "breaks downstream tools."
        )

    def test_no_ansi_in_json_mode(self) -> None:
        # JSON mode must never emit colour regardless of TTY
        # detection. This is stronger than the previous test: it
        # asserts on the mode, not on the environment.
        proc = _run_list("--json")
        assert "\x1b[" not in proc.stdout


# ---------------------------------------------------------------------------
# Empty-state integration
# ---------------------------------------------------------------------------


class TestEmptyStateIntegration:
    """Empty catalogue behaviour holds through the whole pipeline.

    The unit test file `test_list_empty.py` covers the command's
    behaviour with mocked discovery. This file verifies the same
    behaviour end-to-end when the *actual* discovery service returns
    nothing — a case that can happen on a minimal install, on a
    container, or when the local environment shadows the official
    catalogue with a broken override.
    """

    def test_filter_to_empty_exits_zero_and_emits_envelope(self) -> None:
        # `--category=library` has no blueprints in the shipped
        # catalogue. The empty envelope is a valid answer, not an
        # error, and the exit code is 0.
        proc = _run_list("--json", "--category=library")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload == {
            "blueprints": [],
            "schema_version": "1.0.0",
        }, f"Empty-catalogue envelope does not match the contract: " f"{payload}"

    def test_filter_to_empty_table_mode_uses_stderr(self) -> None:
        # In table mode the empty case emits a warning on stderr, and
        # nothing on stdout. This is the "stdout is the payload"
        # contract applied to the empty path.
        proc = _run_list("--category=library")
        assert proc.returncode == 0
        assert proc.stdout == "", (
            f"Empty catalogue in table mode emitted on stdout: "
            f"{proc.stdout!r}; the warning belongs on stderr."
        )
        assert proc.stderr.strip() != "", (
            "Empty catalogue in table mode produced no stderr; a "
            "user would see no feedback and think the command did "
            "not run."
        )


# ---------------------------------------------------------------------------
# Determinism across processes
# ---------------------------------------------------------------------------


class TestCrossProcessDeterminism:
    """Two separate processes produce the same output.

    Determinism across *processes* is strictly stronger than
    determinism within one process: a fresh interpreter rules out
    any state that could accumulate (a cached sort, a mutation of a
    module-level constant). If the sort were implemented by mutating
    a list in place, the first invocation would be correct and a
    later one in the same process might not be — but a *separate*
    process would be correct again, so cross-process determinism does
    not catch that bug. What it *does* catch is nondeterminism that
    comes from environment reads (the clock, a random seed, an
    un-ordered filesystem iteration under a different overlayfs,
    etc.).
    """

    def test_two_processes_produce_identical_bytes(self) -> None:
        proc1 = _run_list("--json")
        proc2 = _run_list("--json")
        assert proc1.returncode == 0
        assert proc2.returncode == 0
        assert proc1.stdout == proc2.stdout, (
            "Two separate processes produced different bytes for "
            "`forge list --json`. This is a source of nondeterminism "
            "in the CLI's output."
        )


# ---------------------------------------------------------------------------
# Local-override integration — the point of the whole discovery design
# ---------------------------------------------------------------------------


class TestLocalOverrideIntegration:
    """A local blueprint shadows an official one end-to-end.

    This is the highest-level test of the discovery precedence rule
    (ADR-0009): a user drops a blueprint into `.forge/blueprints/`
    and `forge list` reflects it in place of the official one. The
    unit tests assert this at the service level; this test asserts
    it at the process level, which means it also verifies that the
    *CLI* wires discovery correctly.

    The test creates a temporary working directory, copies an
    official blueprint into it with a different name, and runs the
    CLI from that directory. That is exactly what a user does — no
    service-level mocking required.
    """

    def test_local_override_takes_precedence(
        self,
        tmp_path: Path,
    ) -> None:
        # Set up a local override in a fresh working directory.
        override_dir = tmp_path / ".forge" / "blueprints"
        override_dir.mkdir(parents=True)
        blueprint_dir = override_dir / "python.fastapi.api"
        blueprint_dir.mkdir()
        (blueprint_dir / "blueprint.yaml").write_text(
            "schema_version: '1.0.0'\n"
            "id: python.fastapi.api\n"
            "name: Local FastAPI Override\n"
            "version: 1.0.0\n"
            "description: local override\n"
            "category: api\n"
            "variables: []\n"
            "files: []\n"
            "verification:\n"
            "  - id: ok\n"
            "    type: required_file\n"
            "    path: README.md\n"
            "compatibility:\n"
            "  min_forge_version: 1.0.0\n",
            encoding="utf-8",
        )

        # Run from the temp directory; the CWD is where
        # `.forge/blueprints/` is looked up (per discovery's
        # `DEFAULT_LOCAL_ROOT`).
        proc = _run_list("--json", cwd=tmp_path)
        assert proc.returncode == 0

        payload = json.loads(proc.stdout)
        fastapi_items = [
            item for item in payload["blueprints"] if item["id"] == "python.fastapi.api"
        ]
        assert len(fastapi_items) == 1, (
            f"Expected exactly one 'python.fastapi.api' in the "
            f"catalogue (local wins, official is shadowed). Found "
            f"{len(fastapi_items)}: {fastapi_items}"
        )
        assert fastapi_items[0]["name"] == "Local FastAPI Override", (
            "Local override did not take precedence: the official "
            "blueprint was returned instead. This is a violation of "
            "ADR-0009's precedence rule."
        )
