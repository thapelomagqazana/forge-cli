"""Contract tests for the `forge list --json` output shape.

What "contract" means here
--------------------------
A contract test asserts on the *public interface* of the CLI, not on
its internals. The JSON emitted by `forge list --json` is consumed by
scripts, CI jobs, and downstream tooling; that makes it an API, and an
API that breaks without warning is worse than one that does not exist.
These tests exist so a change that would break a consumer fails CI
*before* it reaches a release, with a message that names the specific
field or property that changed.

Difference from the unit tests
------------------------------
`tests/unit/cli/test_list_json.py` asserts on the *renderer* and the
*command seam* — it drives `render_json(output)` directly and mocks
the discovery service. Those are fast and focused, but they are
internal-facing: they test how the code is arranged.

This file is different. It drives the *installed CLI* through
`subprocess` (the same way a user would), parses the output, and
asserts on the resulting JSON object. That is the layer where a
contract is actually observable. If the command module is renamed,
the CLI wiring changes, or a dependency is updated in a way that
changes default serialisation behaviour, only these tests catch it.

What this file covers
---------------------
* **Envelope shape**: `schema_version` + `blueprints` at top level.
* **Item shape**: exactly five fields per blueprint, names as documented.
* **Field types**: strings, not reprs or wrapped values.
* **Ordering**: `blueprints[]` sorted by `id` ascending.
* **Determinism**: same invocation → byte-identical output.
* **Purity**: no ANSI escapes, no human framing, one trailing newline.
* **Empty state**: valid envelope, exit 0.
* **Versioned stability**: the `schema_version` field is `"1.0.0"` and
  reflects the shape asserted here.

What this file deliberately does NOT cover
------------------------------------------
* **Table output** — table layout is cosmetic; automation must use
  `--json` (documented in `CLI_LIST_OUTPUT.md`).
* **Discovery correctness** — which blueprints are found is discovery's
  job (see `tests/unit/discovery/`). This file asserts on the *shape*
  of the output, using whatever blueprints happen to be installed.
* **Error paths** — covered in `tests/unit/cli/test_list_empty.py`
  and the general CLI error tests.

Version bump policy
-------------------
If any assertion in this file needs to change, the change is
**consumer-visible**. That means:

1. Update `CLI_LIST_OUTPUT.md`.
2. Bump `ListOutput.schema_version` in
   `forge/cli/schemas/list_output.py` according to its
   major/minor/patch rules.
3. Update the corresponding assertion in this file to expect the new
   version.
4. Announce the change to any downstream consumers.

The failure mode this file is designed to prevent is a silent shape
change: an extra field, a reordered key, a renamed enum member. Those
are invisible to unit tests that build the DTO from the same source
they assert against, but very visible to a `jq` pipeline in
production.

Why subprocess and not `CliRunner`
----------------------------------
`CliRunner` is Typer's in-process test harness; it calls the Typer
app directly, bypassing the entry point (`forge/cli/main.py`) and the
console-script wrapper installed by `pip`. A change to the entry
point — an exception handler that adds an extra line, a version
check that prints a warning — would be invisible to `CliRunner` tests
but would break every real invocation.

`subprocess` runs the same bytes a user runs. That is the layer at
which a CLI contract is observable, and it is the layer this file
tests. The cost is speed (~100ms per invocation instead of ~5ms),
which is acceptable for a file with a dozen assertions.

What this file assumes
----------------------
The installed Forge package contains at least the three official
blueprints. If a future release ships fewer, the assertions on count
and specific ids here will fail — correctly, because that is a
consumer-visible change (scripts keying on `python.fastapi.api` would
break) and it deserves an explicit review.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Invocation helper
# ---------------------------------------------------------------------------

# The tests assume the Forge CLI is runnable as ``python -m forge.cli``.
# That invocation form is guaranteed by ``src/forge/cli/__main__.py``;
# using it (rather than the ``forge`` console script) makes the test
# independent of how the environment was installed (editable, wheel,
# pipx) and of whether ``Scripts/`` is on ``PATH``.
_CLI = (sys.executable, "-m", "forge.cli")

# Repository root, so failure messages can print relative paths.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_list(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m forge.cli list [args]`` and capture the result.

    The returned ``CompletedProcess`` carries ``returncode``,
    ``stdout``, and ``stderr`` — everything a contract test needs to
    assert on. ``text=True`` decodes bytes using the system encoding,
    which matches what a user piping the output through a shell sees.
    """
    return subprocess.run(
        [*_CLI, "list", *args],  # noqa: S603
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )


def _run_list_json() -> tuple[int, dict, str]:
    """Invoke ``forge list --json`` and parse the result.

    Returns a ``(exit_code, parsed_payload, raw_stdout)`` triple.
    Most contract tests want the *parsed* payload (to assert on
    shape) but a few want the raw bytes (to assert on determinism and
    framing). Returning both avoids re-running the CLI for the few
    tests that need both.

    Raises ``AssertionError`` (via ``pytest.fail``) if the invocation
    does not produce parseable JSON on stdout. That failure would mean
    the CLI is broken at a fundamental level — not a contract
    regression — and the assertion message names the raw stdout and
    stderr so a developer can see what happened without re-running.
    """
    proc = _run_list("--json")
    if proc.returncode != 0:
        pytest.fail(
            f"`forge list --json` exited with code {proc.returncode}.\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
            "A non-zero exit here usually means a discovery or "
            "import error, not a contract failure."
        )
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`forge list --json` did not produce valid JSON: {exc}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc.returncode, parsed, proc.stdout


# ---------------------------------------------------------------------------
# Envelope shape — the top-level object
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    """The top-level JSON object is the envelope consumers wrap around.

    An envelope is used (rather than a bare array) so that adding
    top-level metadata later is additive, and so consumers have a
    single place to read `schema_version` from. The keys and their
    types are part of the contract; adding a key is a minor version
    bump, removing one is major.
    """

    def test_top_level_is_an_object_not_an_array(self) -> None:
        # A bare array would still be JSON, but it forecloses every
        # future top-level addition. The choice to use an object is
        # documented; this test fails if someone "simplifies" to a
        # bare array, which would be a major-version break for every
        # consumer.
        _exit, payload, _raw = _run_list_json()
        assert isinstance(payload, dict), (
            "The JSON envelope must be an object, not a bare array. " "See CLI_LIST_OUTPUT.md."
        )

    def test_envelope_has_schema_version(self) -> None:
        _exit, payload, _raw = _run_list_json()
        assert "schema_version" in payload, (
            "The envelope must carry `schema_version` so consumers "
            "can pin behaviour. Its absence is a breaking change."
        )

    def test_schema_version_is_the_current_version(self) -> None:
        # Locked at "1.0.0" for this release. Changing this value is
        # a *deliberate* act that accompanies a shape change and a
        # corresponding update to CLI_LIST_OUTPUT.md. This test fails
        # so the value cannot drift silently.
        _exit, payload, _raw = _run_list_json()
        assert payload["schema_version"] == "1.0.0", (
            f"`schema_version` is {payload['schema_version']!r}, "
            f"expected '1.0.0'. If the output shape has changed, "
            f"bump the version in forge/cli/schemas/list_output.py "
            f"and update CLI_LIST_OUTPUT.md."
        )

    def test_envelope_has_blueprints_array(self) -> None:
        _exit, payload, _raw = _run_list_json()
        assert "blueprints" in payload
        assert isinstance(payload["blueprints"], list), "`blueprints` must be a JSON array."

    def test_envelope_top_level_keys_are_exactly_the_contract(self) -> None:
        # Equality, not subset. An accidental extra top-level key
        # (say, a debug `generated_at` timestamp) would be visible to
        # consumers and is a minor-version-bump change; asserting
        # equality means the change fails CI before it ships.
        _exit, payload, _raw = _run_list_json()
        assert set(payload) == {"schema_version", "blueprints"}, (
            f"Unexpected top-level keys in envelope: "
            f"{set(payload) - {'schema_version', 'blueprints'}}. "
            f"Adding a key is a minor-version change; removing or "
            f"renaming is major. See CLI_LIST_OUTPUT.md."
        )


# ---------------------------------------------------------------------------
# Item shape — the per-blueprint object
# ---------------------------------------------------------------------------


class TestItemShape:
    """Every item in ``blueprints[]`` has exactly the contract's fields."""

    def test_item_fields_are_exactly_the_contract(self) -> None:
        # The five fields are what consumers read. Subset assertions
        # ("at least these fields are present") would allow an extra
        # field to ship silently; equality is the contract.
        _exit, payload, _raw = _run_list_json()
        expected = {"id", "name", "version", "category", "status"}
        for item in payload["blueprints"]:
            assert set(item) == expected, (
                f"Unexpected fields in a blueprint item: "
                f"{set(item) ^ expected}. The five fields are fixed "
                f"for schema_version 1.0.0. See CLI_LIST_OUTPUT.md."
            )

    def test_item_id_is_a_string(self) -> None:
        _exit, payload, _raw = _run_list_json()
        for item in payload["blueprints"]:
            assert isinstance(item["id"], str), (
                f"`id` must serialise as a string, got "
                f"{type(item['id']).__name__}: {item['id']!r}"
            )

    def test_item_name_is_a_string(self) -> None:
        _exit, payload, _raw = _run_list_json()
        for item in payload["blueprints"]:
            assert isinstance(item["name"], str)

    def test_item_version_is_a_semver_string(self) -> None:
        # Consumers keying on version want a string they can parse
        # with a semver library; a `{"major": 1, ...}` object would
        # be a completely different shape.
        _exit, payload, _raw = _run_list_json()
        import re

        semver = re.compile(
            r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
        )
        for item in payload["blueprints"]:
            assert isinstance(item["version"], str), (
                f"`version` must be a string, got " f"{type(item['version']).__name__}"
            )
            assert semver.match(
                item["version"]
            ), f"`version` must be SemVer, got {item['version']!r}"

    def test_item_category_is_a_known_string(self) -> None:
        _exit, payload, _raw = _run_list_json()
        allowed = {"api", "automation", "frontend", "library", "cli", "other"}
        for item in payload["blueprints"]:
            assert isinstance(item["category"], str), (
                "`category` must serialise as a string, not an enum " "repr or wrapped object"
            )
            assert item["category"] in allowed, (
                f"`category` value {item['category']!r} is not in "
                f"the allowed set {allowed}. Adding a category is a "
                f"minor-version change and requires a schema update."
            )

    def test_item_status_is_a_known_string(self) -> None:
        _exit, payload, _raw = _run_list_json()
        allowed = {"ok", "stale", "error"}
        for item in payload["blueprints"]:
            assert isinstance(
                item["status"], str
            ), "`status` must serialise as a string, not an enum repr"
            assert item["status"] in allowed, (
                f"`status` value {item['status']!r} is not in "
                f"{allowed}. If a new status is needed, add it to "
                f"forge/models/status.py *and* update "
                f"CLI_LIST_OUTPUT.md."
            )

    def test_no_enum_repr_leaks_into_values(self) -> None:
        # The classic failure: a `str`-subclassed enum that is
        # serialised via a path that bypasses Pydantic's JSON mode
        # produces `"Category.api"` or `"<Category.api: 'api'>"`
        # rather than `"api"`. The per-field assertions above already
        # catch this (they would see a string that is not in the
        # allowed set), but this assertion is a safety net that names
        # the failure mode explicitly for a reviewer.
        _exit, payload, _raw = _run_list_json()
        for item in payload["blueprints"]:
            for field in ("category", "status"):
                value = item[field]
                assert "." not in value, (
                    f"`{field}` value {value!r} looks like an enum "
                    f"repr, not a plain string. Ensure the DTO uses "
                    f"`model_dump(mode='json')` (see "
                    f"forge/cli/renderers/json.py)."
                )


# ---------------------------------------------------------------------------
# Ordering — a documented guarantee
# ---------------------------------------------------------------------------


class TestOrdering:
    """`blueprints[]` is sorted by `id` ascending.

    `CLI_LIST_OUTPUT.md` promises this. Consumers may rely on it for
    the current `schema_version`. A change to the ordering — even to
    a "better" ordering — is a consumer-visible change and needs the
    same review as a field-shape change.
    """

    def test_blueprints_are_sorted_by_id(self) -> None:
        _exit, payload, _raw = _run_list_json()
        ids = [item["id"] for item in payload["blueprints"]]
        assert ids == sorted(ids), (
            f"`blueprints[]` is not sorted by `id`. Got: {ids}. "
            f"The sort is part of the contract "
            f"(CLI_LIST_OUTPUT.md); see ADR-0012 for why sorting "
            f"happens in the command, not the renderer."
        )

    def test_sort_is_lexicographic_not_length_based(self) -> None:
        # A sort key of `len(id)` or `id.split('.')[-1]` would produce
        # a different order for the shipped catalogue than a
        # lexicographic sort by id. The shipped ids are:
        #
        #     python.automation.cli   (21 chars)
        #     python.fastapi.api      (17 chars)
        #     typescript.frontend.react (25 chars)
        #
        # Lexicographic:  python.automation.cli, python.fastapi.api,
        #                 typescript.frontend.react
        # By length:      python.fastapi.api, python.automation.cli,
        #                 typescript.frontend.react
        # By last seg:    api, api, react
        #
        # The three orderings are distinguishable. This test pins the
        # correct one on the actual installed catalogue.
        _exit, payload, _raw = _run_list_json()
        ids = [item["id"] for item in payload["blueprints"]]
        # The exact expected order for the shipped catalogue. If the
        # shipped catalogue changes, this assertion updates too —
        # which is the point: the *ordering* of the shipped ids is
        # part of what consumers can rely on.
        official_prefix = {"python.", "typescript."}
        if not all(any(i.startswith(p) for p in official_prefix) for i in ids):
            pytest.skip("custom blueprints installed; ordering test is for official catalogue")

        assert ids == [
            "python.automation.cli",
            "python.fastapi.api",
            "typescript.frontend.react",
        ], (
            "The official catalogue's ids are not in the "
            "lexicographic order consumers expect. The shipped "
            f"catalogue currently has: {ids}."
        )


# ---------------------------------------------------------------------------
# Determinism — a documented guarantee
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Two invocations produce byte-identical output."""

    def test_two_runs_produce_identical_bytes(self) -> None:
        # The golden fixtures under tests/fixtures/list/ depend on
        # byte-level determinism. A change that introduces any
        # nondeterminism (a set, a dict with insertion-order ties, an
        # unsorted filesystem iteration) would break those fixtures
        # *and* any consumer diffing output between runs.
        _exit1, _payload1, raw1 = _run_list_json()
        _exit2, _payload2, raw2 = _run_list_json()
        assert raw1 == raw2, (
            "Two invocations of `forge list --json` produced "
            "different bytes. This breaks both the golden fixtures "
            "and any consumer doing byte-level comparison."
        )

    def test_two_runs_produce_same_sorted_ids(self) -> None:
        # Weaker than byte-identical but more informative on failure:
        # if determinism breaks, this test names *what* differed
        # (the ordering) rather than just "the bytes".
        _exit1, payload1, _raw1 = _run_list_json()
        _exit2, payload2, _raw2 = _run_list_json()
        ids1 = [item["id"] for item in payload1["blueprints"]]
        ids2 = [item["id"] for item in payload2["blueprints"]]
        assert ids1 == ids2, (
            f"Two invocations produced different orderings:\n"
            f"  run 1: {ids1}\n"
            f"  run 2: {ids2}"
        )


# ---------------------------------------------------------------------------
# Purity — no framing mixed with the payload
# ---------------------------------------------------------------------------


class TestPurity:
    """The stdout of `--json` is a clean payload, nothing else."""

    def test_no_ansi_escape_sequences(self) -> None:
        # ANSI escapes are bytes; they would survive JSON parsing on
        # some paths and break consumers that read raw output. The
        # renderer is documented as ANSI-free; this test verifies the
        # whole pipeline (not just the renderer).
        _exit, _payload, raw = _run_list_json()
        assert "\x1b[" not in raw, (
            "ANSI escape sequence found in `--json` stdout. Colour "
            "belongs to `ui.emit`, never to the payload."
        )

    def test_no_human_framing(self) -> None:
        # Table-mode framing must not leak into `--json` stdout:
        # section headers, success checkmarks, warning prefixes. Each
        # is a distinct string that a table-mode regression would
        # introduce.
        _exit, _payload, raw = _run_list_json()
        for marker in ("Available blueprints", "✓", "⚠", "✗"):
            assert marker not in raw, (
                f"Table-mode marker {marker!r} found in `--json` "
                f"stdout. Table framing and JSON output must not "
                f"share a stream."
            )

    def test_ends_with_exactly_one_newline(self) -> None:
        # `ui.emit` owns the trailing newline. Exactly one — not zero
        # (a shell prompt glued to the output) and not two (a blank
        # line that breaks `wc -l` expectations).
        _exit, _payload, raw = _run_list_json()
        assert raw.endswith("\n"), (
            "`--json` stdout must end with a newline; a POSIX text "
            "file ends with one, and a missing newline glues the "
            "shell prompt to the payload."
        )
        assert not raw.endswith("\n\n"), (
            "`--json` stdout ends with two newlines; `ui.emit` " "appends exactly one."
        )

    def test_no_leading_whitespace_before_json(self) -> None:
        # A leading blank line (from a stray `print()` in a handler)
        # would still parse with `json.loads` after `.strip()`, but
        # would break line-count assertions and look odd when piped
        # into `head`.
        _exit, _payload, raw = _run_list_json()
        assert not raw.startswith(("\n", " ", "\t")), (
            "`--json` stdout has leading whitespace before the " "JSON payload."
        )


# ---------------------------------------------------------------------------
# Empty state — a valid envelope, not an error
# ---------------------------------------------------------------------------


class TestEmptyState:
    """An empty catalogue still produces a valid envelope.

    The empty case is easy to get wrong in two ways:

    * Returning early without calling the renderer (empty stdout).
    * Emitting a human-facing "No blueprints found." message instead
      of the JSON envelope.

    Both would break `jq '.blueprints | length'`, which should return
    `0` — not a parse error, not a string. `CLI_LIST_OUTPUT.md`
    documents this; ADR-0012 explains why it is a design goal
    ("emptiness is a valid answer").
    """

    def test_empty_catalogue_emits_valid_envelope(self) -> None:
        # Force the empty case by filtering to a category that has no
        # blueprints. The shipped catalogue has none in "library".
        # If a future release ships a "library" blueprint, this test
        # becomes vacuous — update the filter then.
        proc = _run_list("--json", "--category=library")
        assert proc.returncode == 0, (
            f"Empty catalogue must exit 0; got {proc.returncode}.\n" f"stderr: {proc.stderr}"
        )
        parsed = json.loads(proc.stdout)  # raises if stdout is empty
        assert parsed == {"blueprints": [], "schema_version": "1.0.0"}, (
            f"Empty catalogue envelope does not match the contract: " f"{parsed}"
        )

    def test_empty_catalogue_exits_zero_in_table_mode(self) -> None:
        # Same rule for table mode: empty is a valid answer, not a
        # failure. A `set -e` script that runs `forge list` should
        # succeed on a minimal install.
        proc = _run_list("--category=library")
        assert proc.returncode == 0
        # The warning goes to stderr; stdout is empty (no table).
        assert proc.stdout == "", (
            f"Empty catalogue in table mode emitted stdout: "
            f"{proc.stdout!r}. The message belongs on stderr."
        )


# ---------------------------------------------------------------------------
# Versioned stability — pin the contract's own declaration
# ---------------------------------------------------------------------------


class TestVersionedStability:
    """The `schema_version` field and the shape it governs agree.

    This test is deliberately a *meta-test*: it reads the version out
    of the CLI's own output and checks that the shape that version
    promises is the shape observed. When the shape changes, both the
    version and this test must be updated together — which forces the
    coupling to be visible in a single diff.
    """

    def test_version_and_shape_are_consistent(self) -> None:
        _exit, payload, _raw = _run_list_json()
        version = payload["schema_version"]

        # Rules for v1.0.0: two top-level keys, five item fields.
        if version == "1.0.0":
            assert set(payload) == {"schema_version", "blueprints"}
            for item in payload["blueprints"]:
                assert set(item) == {
                    "id",
                    "name",
                    "version",
                    "category",
                    "status",
                }
        else:
            pytest.fail(
                f"Unknown schema_version {version!r}. Every version "
                f"the CLI emits must be covered by a branch here so "
                f"consumers of *that* version can rely on the "
                f"assertions above."
            )

    def test_version_follows_semver(self) -> None:
        # Same check as `test_item_version_is_a_semver_string`, but
        # applied to the envelope's own version. Consistency between
        # the two is not required (they are different contract
        # surfaces), but using SemVer for both avoids confusion.
        import re

        _exit, payload, _raw = _run_list_json()
        semver = re.compile(
            r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
        )
        assert semver.match(payload["schema_version"]), (
            f"`schema_version` must be SemVer, got " f"{payload['schema_version']!r}"
        )
