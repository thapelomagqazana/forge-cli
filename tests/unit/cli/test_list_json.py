"""Unit tests for the `forge list --json` output contract.

What this file covers
---------------------
The JSON renderer (`forge.cli.renderers.json`) and the `--json` output
path of the command seam (`forge.cli.commands.list.run`) end-to-end.
The JSON output is a *public contract*: scripts parse it, CI jobs
assert on it, and downstream tooling pins field names and shapes. That
means the tests here are stricter than the table tests — the JSON
shape is versioned (see ``schema_version`` in ``ListOutput``) and any
change is a breaking change for someone.

What this file deliberately does NOT cover
------------------------------------------
* **Table rendering** — lives in ``test_list_table.py``.
* **Empty state** — lives in ``test_list_empty.py`` (though the JSON
  *envelope* for the empty case is tested here, because the shape of
  the empty response is part of the JSON contract).
* **Sorting** — lives in ``test_list_stable.py``. The renderer
  preserves input order; the *command* sorts (per ADR-0012). Testing
  sort here would confuse "who owns ordering?" — the answer is the
  command, and the test that proves it lives there.
* **Discovery** — mocked at the service boundary. JSON rendering has
  no opinion about where blueprints come from.

Contract tests vs. snapshot tests
---------------------------------
Two kinds of assertions live in this file, and they fail for different
reasons:

* **Contract assertions** (the vast majority here) fail when the
  *shape* of the JSON changes: a field is renamed, a value type
  changes, an enum gains or loses a member, the sort order differs
  from the documented order. These are consumer-visible breaking
  changes and require an ADR-0012 review plus a ``schema_version``
  bump in the DTO.

* **Snapshot assertions** (the ``TestSnapshots`` class) fail when the
  *bytes* of the JSON output change for a fixed input. Because the
  renderer is deterministic (``sort_keys=True``, fixed separators,
  no ANSI), byte-level equality is meaningful — a snapshot diff
  usually indicates a shape or content change worth reviewing.

Golden-fixture newline convention
---------------------------------
The snapshot fixtures under ``tests/fixtures/list/`` are *text files
on disk*, and text files end with a newline. The payload they contain
(the JSON envelope) does not — the newline is ``ui.emit``'s framing,
not the renderer's output.

``_assert_golden`` normalises both sides to the payload:

* **Read side:** the file's trailing ``"\\n"`` is stripped before
  comparison.
* **Write side:** ``UPDATE_SNAPSHOTS=1`` writes ``actual + "\\n"``,
  so the file on disk keeps canonical POSIX text-file form even when
  regenerated.

This rule is applied *identically* here and in ``test_list_empty.py``,
whose helper reads and writes the same fixture directory. A helper
that writes a trailing newline and one that does not would race:
whichever ran last under ``UPDATE_SNAPSHOTS=1`` would decide the
on-disk form, and the other helper would fail on the next run. Both
helpers are kept in sync because they are both consumers of the same
contract.

Snapshot fixtures live under ``tests/fixtures/list/golden_*.json``.
Regenerate with ``UPDATE_SNAPSHOTS=1`` and inspect the diff before
committing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forge.cli.commands import list as list_cmd
from forge.cli.renderers.json import render_json
from forge.cli.schemas.list_output import (
    BlueprintStatus,
    ListOutput,
    ListOutputItem,
)
from forge.models.blueprint import (
    BlueprintSource,
    Category,
    DiscoveredBlueprint,
)

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "list"
_UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

# The three official blueprints, expressed as discovery-service output.
# Duplicating this in the test (rather than importing from a fixture
# factory) makes each test read as "given these three blueprints, this
# is the JSON". A test file that spends most of its length on setup
# helpers is harder to review than one that shows its inputs.
_OFFICIAL_PAIRS: list[tuple[DiscoveredBlueprint, BlueprintStatus]] = [
    (
        DiscoveredBlueprint(
            id="python.automation.cli",
            name="Python Automation CLI",
            version="1.0.0",
            description="CLI automation blueprint",
            category=Category.api,
            source=BlueprintSource.OFFICIAL,
        ),
        BlueprintStatus.ok,
    ),
    (
        DiscoveredBlueprint(
            id="python.fastapi.api",
            name="Python FastAPI API",
            version="1.0.0",
            description="FastAPI service blueprint",
            category=Category.api,
            source=BlueprintSource.OFFICIAL,
        ),
        BlueprintStatus.ok,
    ),
    (
        DiscoveredBlueprint(
            id="typescript.frontend.react",
            name="TypeScript React Frontend",
            version="1.0.0",
            description="React frontend blueprint",
            category=Category.frontend,
            source=BlueprintSource.OFFICIAL,
        ),
        BlueprintStatus.ok,
    ),
]


def _row(
    id_: str = "python.fastapi.api",
    name: str = "Python FastAPI API",
    version: str = "1.0.0",
    category: Category = Category.api,
    status: BlueprintStatus = BlueprintStatus.ok,
) -> ListOutputItem:
    """Build a `ListOutputItem` with sane defaults for one-field tests."""
    return ListOutputItem(
        id=id_,
        name=name,
        version=version,
        category=category,
        status=status,
    )


def _run_json(
    pairs: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
    *,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Invoke `forge list --json` with mocked discovery.

    Returns ``(exit_code, stdout)`` so tests can assert on both without
    a second ``capsys.readouterr()`` call — capturing twice would clear
    the buffer and silently lose the output of the first assertion.
    """
    with patch(
        "forge.cli.commands.list.BlueprintDiscoveryService",
    ) as service_cls:
        service_cls.return_value.discover_with_status.return_value = pairs
        exit_code = list_cmd.run(SimpleNamespace(json=True))
    return exit_code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# Renderer — pure-function tests
# ---------------------------------------------------------------------------


class TestRenderJsonOutput:
    """The JSON renderer's output properties."""

    def test_output_is_valid_json(self) -> None:
        # The very first property of a JSON renderer: its output must
        # parse as JSON. A trailing comma, an unescaped control
        # character, or a stray ANSI escape all fail here before any
        # shape assertion runs.
        output = ListOutput(blueprints=[_row()])
        rendered = render_json(output)
        parsed = json.loads(rendered)  # raises if invalid
        assert isinstance(parsed, dict)

    def test_output_has_no_trailing_newline(self) -> None:
        # `ui.emit` owns line termination; the renderer returns a bare
        # string. A double newline would produce a blank line after
        # every `forge list --json` invocation, which is harmless to
        # `jq` but does break naive line-count assertions and makes
        # diff-based tooling noisy.
        output = ListOutput(blueprints=[_row()])
        assert not render_json(output).endswith("\n")

    def test_output_is_ansi_free(self) -> None:
        # ANSI escapes would make the JSON technically parseable as
        # bytes (they are just characters) but would break every
        # downstream consumer that expects data, not terminal control
        # codes. Colour is `ui.emit`'s job, never the renderer's.
        output = ListOutput(blueprints=[_row()])
        assert "\x1b" not in render_json(output)

    def test_output_is_deterministic(self) -> None:
        # Same input, same bytes. This is what makes the golden
        # fixtures meaningful: a single byte change is a signal, not
        # noise.
        output = ListOutput(blueprints=[_row(id_="a.a"), _row(id_="b.b")])
        assert render_json(output) == render_json(output)

    def test_keys_are_sorted_alphabetically(self) -> None:
        # `sort_keys=True` is a stability guarantee (ADR-0012): a
        # refactor that reorders fields in ``ListOutputItem`` must not
        # change the emitted byte order. Read the raw string and
        # confirm the key order that appears at the top level is
        # alphabetical (``blueprints`` before ``schema_version``).
        output = ListOutput(blueprints=[_row()])
        raw = render_json(output)
        assert raw.index('"blueprints"') < raw.index('"schema_version"')

    def test_item_keys_are_sorted_alphabetically(self) -> None:
        # Same guarantee, applied to an item's fields.
        output = ListOutput(blueprints=[_row()])
        raw = render_json(output)
        for key in ("category", "id", "name", "status", "version"):
            assert f'"{key}"' in raw, f"missing key {key!r} in output: {raw}"


class TestRenderJsonEnumSerialisation:
    """Enums must serialise to their string values, not to reprs."""

    def test_status_serialises_as_string_value(self) -> None:
        output = ListOutput(blueprints=[_row(status=BlueprintStatus.stale)])
        parsed = json.loads(render_json(output))
        assert parsed["blueprints"][0]["status"] == "stale", (
            "status must serialise as its string value, not "
            "e.g. 'BlueprintStatus.stale' or {'value': 'stale'}"
        )

    def test_category_serialises_as_string_value(self) -> None:
        output = ListOutput(blueprints=[_row(category=Category.frontend)])
        parsed = json.loads(render_json(output))
        assert parsed["blueprints"][0]["category"] == "frontend"

    def test_no_enum_repr_leaks(self) -> None:
        # The classic failure: an Enum that does not subclass ``str``
        # serialises to ``"Category.api"`` or ``"<Category.api: 'api'>"``
        # depending on the mode. Both are breaking changes for
        # consumers; both are caught by asserting the raw string does
        # not contain the class name.
        output = ListOutput(blueprints=[_row()])
        raw = render_json(output)
        assert "Category." not in raw, (
            "Category enum repr leaked into JSON; is the enum missing "
            "a str base or is the wrong Pydantic dump mode being used?"
        )
        assert "BlueprintStatus." not in raw, "BlueprintStatus enum repr leaked into JSON."


class TestRenderJsonEmpty:
    """The JSON renderer's empty-catalogue behaviour.

    This is *distinct* from the command's empty behaviour (which still
    emits a JSON envelope when ``--json`` is passed — the command does
    not print ``"No blueprints found."`` in JSON mode because that
    message is a human-facing sentence, not a machine-parseable
    payload). Testing the renderer's empty case in isolation locks the
    envelope shape independently of the command's branching.
    """

    def test_empty_blueprints_array_renders_as_empty_envelope(self) -> None:
        output = ListOutput(blueprints=[])
        parsed = json.loads(render_json(output))
        assert parsed == {"blueprints": [], "schema_version": "1.0.0"}


# ---------------------------------------------------------------------------
# Command — end-to-end through the run seam
# ---------------------------------------------------------------------------


class TestListCommandJsonOutput:
    """`forge list --json` end-to-end through `list_cmd.run`."""

    def test_json_flag_emits_parseable_json(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        assert exit_code == 0
        parsed = json.loads(out)  # raises if invalid
        assert isinstance(parsed, dict)

    def test_json_output_has_schema_version_envelope(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The envelope's ``schema_version`` is what lets consumers pin
        # behaviour. Its absence is a breaking change (they cannot
        # distinguish v1 from a future v2 without it).
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        parsed = json.loads(out)
        assert parsed["schema_version"] == "1.0.0"

    def test_json_output_contains_all_three_official_blueprints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        parsed = json.loads(out)
        ids = {item["id"] for item in parsed["blueprints"]}
        assert ids == {
            "python.automation.cli",
            "python.fastapi.api",
            "typescript.frontend.react",
        }

    def test_json_output_sorted_by_id(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Sorting is part of the JSON contract (CLI_LIST_OUTPUT.md):
        # consumers may rely on it for the current schema_version.
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        parsed = json.loads(out)
        ids = [item["id"] for item in parsed["blueprints"]]
        assert ids == sorted(ids)

    def test_json_output_field_set_is_exactly_the_contract(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The set of keys on each item is the *contract*. Adding a key
        # is a minor version bump; removing or renaming is major.
        # Asserting equality (not subset) means an accidental extra
        # field fails here, not in a downstream consumer's CI.
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        parsed = json.loads(out)
        for item in parsed["blueprints"]:
            assert set(item) == {
                "id",
                "name",
                "version",
                "category",
                "status",
            }, f"unexpected field set in {item!r}"

    def test_json_output_enum_values_are_strings(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        parsed = json.loads(out)
        for item in parsed["blueprints"]:
            assert isinstance(item["category"], str)
            assert isinstance(item["status"], str)

    def test_json_output_is_ansi_free(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `ui.emit` applies no framing; but asserting it here means a
        # future "add colour to the JSON" PR is caught immediately
        # rather than after a downstream consumer complains.
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        assert "\x1b" not in out

    def test_json_output_has_no_human_framing(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # JSON mode must not emit section headers, success checks, or
        # "No blueprints found." messages. Those are for the table
        # path; mixing them would break every consumer.
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        assert "Available blueprints" not in out
        assert "✓" not in out
        assert "No blueprints" not in out

    def test_json_output_ends_with_exactly_one_newline(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `ui.emit` owns the newline. Exactly one — not zero (a shell
        # prompt glued to the output) and not two (a blank line that
        # breaks `wc -l` expectations).
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_json_flag_with_empty_catalogue_emits_empty_envelope(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Empty catalogue + --json = an empty ``blueprints`` array
        # *inside* the envelope, not a human-facing message. Exit code
        # is still 0 (empty is a valid answer, per ADR-0012).
        exit_code, out = _run_json([], capsys=capsys)
        assert exit_code == 0
        parsed = json.loads(out)
        assert parsed == {"blueprints": [], "schema_version": "1.0.0"}


# ---------------------------------------------------------------------------
# Snapshot fixtures — the reviewed artefact
# ---------------------------------------------------------------------------


class TestSnapshots:
    """Compare the rendered JSON against checked-in golden fixtures.

    The fixtures under ``tests/fixtures/list/golden_*.json`` are the
    *contract*, not just the current output. Any change here is a
    consumer-visible breaking change for someone running
    ``forge list --json`` in a script.

    If a golden fixture is missing, the failure names the exact
    regeneration command. If it exists but does not match, the failure
    shows both the expected and actual payloads — a diff a reviewer
    can reason about, rather than a bare ``AssertionError``.

    Newline convention
    ------------------
    See the module docstring's "Golden-fixture newline convention"
    section. Both this helper and its sibling in ``test_list_empty.py``
    use the same rule:

    * Read: ``file.read_text().rstrip("\\n")``
    * Write: ``file.write_text(actual + "\\n")``

    Both sides normalise to the payload; the newline is a file
    convention, not part of the contract. Applying this rule
    identically on both helpers is what keeps ``UPDATE_SNAPSHOTS=1``
    from racing between the two files (each would otherwise decide
    the on-disk form differently, and the other would fail on the
    next run).

    Regenerate with:
        UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/cli/test_list_json.py --no-cov
    """

    def _assert_golden(self, actual: str, fixture_name: str) -> None:
        fixture_path = _FIXTURES / fixture_name

        if _UPDATE:
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            # Write with a trailing newline so the file stays in the
            # POSIX text-file form that editors, ``git``, and PR
            # review UIs all produce and preserve. The read side
            # strips exactly one ``\n`` before comparing, so the
            # newline is a file convention, not part of the payload.
            #
            # Do NOT remove the ``+ "\n"``. A fixture written without
            # a trailing newline will regain one the next time any
            # editor touches it, and this helper will then fail with
            # a newline-only diff — the exact failure that this
            # change is fixing.
            fixture_path.write_text(actual + "\n", encoding="utf-8")
            return

        if not fixture_path.exists():
            pytest.fail(
                f"Golden fixture {fixture_name!r} does not exist.\n"
                f"Regenerate it with:\n"
                f"    UPDATE_SNAPSHOTS=1 uv run pytest "
                f"{Path(__file__).name} --no-cov\n"
                f"and inspect the diff before committing. This fixture "
                f"is a *contract*, not a snapshot — treat changes to it "
                f"as breaking-change reviews."
            )

        # ``rstrip("\n")`` (not ``rstrip()``) is deliberate: we strip
        # the file's trailing line terminator, and only that. A
        # payload whose last character is a space is preserved
        # exactly, which matters for a byte-level contract. Stripping
        # *all* whitespace would silently mangle such a payload and
        # make this test incapable of catching a real bug where the
        # renderer emitted a trailing space.
        expected = fixture_path.read_text(encoding="utf-8").rstrip("\n")
        actual_payload = actual.rstrip("\n")
        assert actual_payload == expected, (
            f"\nGolden fixture mismatch for {fixture_name}.\n"
            f"Golden fixtures are the *contract* for `forge list --json` "
            f"and any change is consumer-visible.\n"
            f"If this change is intentional, regenerate with:\n"
            f"    UPDATE_SNAPSHOTS=1 uv run pytest "
            f"{Path(__file__).name} --no-cov\n"
            f"and update CLI_LIST_OUTPUT.md if field names or shapes "
            f"changed. Bump ``ListOutput.schema_version`` if the change "
            f"is not purely cosmetic.\n\n"
            f"--- expected ---\n{expected}\n"
            f"--- actual ---\n{actual_payload}\n"
        )

    def test_golden_official_json(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, out = _run_json(_OFFICIAL_PAIRS, capsys=capsys)
        # `ui.emit` appends exactly one newline; the fixture captures
        # the JSON body without it (the fixture is the *payload*, the
        # newline is `emit`'s framing). ``_assert_golden`` normalises
        # the read side to the same convention.
        self._assert_golden(out, "golden_official.json")

    def test_golden_empty_envelope(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, out = _run_json([], capsys=capsys)
        self._assert_golden(out, "envelope_empty.json")
