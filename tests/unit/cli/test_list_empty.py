"""Unit tests for the `forge list` empty-catalogue behaviours.

What this file covers
---------------------
The command's behaviour when the catalogue is empty (or when filters
narrow it to nothing), in both output modes:

* **Table mode** — a human-facing warning on stderr, exit 0, no table.
* **JSON mode** — a valid empty envelope on stdout, exit 0, no warning.
* **Filter-narrowed vs. install-empty** — the two distinct empty cases
  get distinct messages in table mode, because their recovery differs.

What this file deliberately does NOT cover
------------------------------------------
* **The renderer's empty case** (`render_table([])`) — that is a
  *renderer* property, tested in `test_list_table.py`. This file tests
  the *command*'s empty branches, which happen *before* the renderer
  is called.
* **Table rendering with rows** — `test_list_table.py`.
* **JSON rendering with rows** — `test_list_json.py`.
* **Sorting** — `test_list_stable.py`. Ordering is irrelevant when
  there are no rows.

Why empty-catalogue behaviour gets its own file
-----------------------------------------------
Empty states are where CLIs most often get the exit-code contract
wrong. The recurring errors are:

* exiting 1 because "the list is empty" *feels* like failure — it is
  not; the command answered the question ("what blueprints are
  available?" → "none") with a valid answer;
* emitting a human-facing message ("No blueprints found.") in `--json`
  mode — which breaks every consumer that pipes the output into `jq`;
* emitting *nothing* in `--json` mode — which breaks the same
  consumers, but with a more confusing error (`JSONDecodeError:
  Expecting value` instead of a clean empty array);
* using one message for both "nothing installed" and "nothing matched
  your filter" — which sends the user looking for the wrong fix.

Each of those has a test here. The point is not to test "empty works";
the point is to lock the *specific contract* that each empty case
promises, because every one of them has been silently broken in some
CLI somewhere.

Contract reference
------------------
See `docs/reference/CLI_LIST_OUTPUT.md` for the JSON envelope and
`docs/adr/0012-cli-output-contracts-and-renderers.md` for the
"emptiness is success" rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forge.cli.commands import list as list_cmd
from forge.models.blueprint import (
    BlueprintSource,
    Category,
    DiscoveredBlueprint,
)
from forge.models.status import BlueprintStatus

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "list"


def _discovered(
    id_: str = "python.fastapi.api",
    name: str = "Python FastAPI API",
    version: str = "1.0.0",
    category: Category = Category.api,
    source: BlueprintSource = BlueprintSource.OFFICIAL,
) -> DiscoveredBlueprint:
    """Build a `DiscoveredBlueprint` -- the discovery service's return type."""
    return DiscoveredBlueprint(
        id=id_,
        name=name,
        version=version,
        description="d",
        category=category,
        source=source,
    )


def _run_list(
    pairs: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
    *,
    json_mode: bool = False,
    category: str = "all",
    source: str = "all",
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Invoke `forge list` with mocked discovery; return exit code, stdout, stderr.

    Returns all three so a test can assert on exit code, stdout, and
    stderr in one call. Calling ``capsys.readouterr()`` twice would
    clear the buffer between calls, silently losing the first capture
    -- a common source of "the test passes but does not check what I
    thought it checked". Returning all three once makes that mistake
    impossible.
    """
    with patch(
        "forge.cli.commands.list.BlueprintDiscoveryService",
    ) as service_cls:
        service_cls.return_value.discover_with_status.return_value = pairs
        exit_code = list_cmd.run(SimpleNamespace(json=json_mode, category=category, source=source))
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Empty catalogue -- table mode
# ---------------------------------------------------------------------------


class TestEmptyCatalogueTableMode:
    """Empty catalogue, no `--json` flag.

    The command's obligation is:

    1. Exit 0 (empty is a valid answer, per ADR-0012).
    2. Print *no* table on stdout (the renderer is not called).
    3. Print a human-facing warning on stderr explaining the recovery.

    All three matter. Exiting 0 without a message leaves the user
    wondering if the command ran; printing the message on stdout would
    pollute the payload for any consumer piping the output; exiting
    non-zero would break `set -e` scripts on a perfectly valid query.
    """

    def test_exits_zero_with_no_blueprints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code, _out, _err = _run_list([], capsys=capsys)
        assert exit_code == 0, (
            "Empty catalogue must exit 0. A non-zero exit would tell "
            "shell scripts (`set -e`) that the query failed, when the "
            "query in fact succeeded with an empty answer (ADR-0012)."
        )

    def test_no_table_on_stdout(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, out, _err = _run_list([], capsys=capsys)
        assert out == "", (
            "Table mode with an empty catalogue must not emit a header "
            "or separator to stdout. The user sees a message, not a "
            "skeleton table."
        )

    def test_warning_on_stderr(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, _out, err = _run_list([], capsys=capsys)
        assert err != "", (
            "Empty catalogue must produce a warning on stderr; otherwise "
            "the user sees no output and cannot tell whether the command "
            "ran."
        )

    def test_warning_mentions_installing_or_doctor(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The message is not the contract; its *information content*
        # is. A user with an empty catalogue has two paths forward:
        # install a blueprint locally, or check that the installation
        # is intact. The message must name at least one of them.
        _exit, _out, err = _run_list([], capsys=capsys)
        assert any(
            hint in err.lower() for hint in ("installation", "forge doctor", ".forge/blueprints")
        ), f"Empty-catalogue warning must name a recovery path; " f"got: {err!r}"


# ---------------------------------------------------------------------------
# Empty catalogue -- JSON mode
# ---------------------------------------------------------------------------


class TestEmptyCatalogueJsonMode:
    """Empty catalogue with `--json`.

    This is the case that gets broken most often, in two distinct ways:

    * The command emits a human-facing warning ("No blueprints found.")
      instead of the JSON envelope. Every consumer piping the output
      into `jq` gets a parse error.
    * The command returns *before* calling the renderer, so stdout is
      empty. Same parse error, but with a less obvious cause.

    The contract is unambiguous: `--json` means "I asked for a
    payload, give me a payload." An empty catalogue is still a
    payload — the empty envelope — and the only thing on stderr
    should be operational diagnostics (in the current
    implementation, nothing).
    """

    def test_exits_zero_with_empty_envelope(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code, _out, _err = _run_list([], json_mode=True, capsys=capsys)
        assert exit_code == 0

    def test_stdout_is_parseable_json(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # This is the test that catches both "returned early without
        # rendering" and "emitted a human message instead of a
        # payload". `json.loads` fails on both, and the failure
        # message is the same `JSONDecodeError: Expecting value`,
        # which points straight at the missing or malformed payload.
        _exit, out, _err = _run_list([], json_mode=True, capsys=capsys)
        parsed = json.loads(out)  # raises if stdout is empty or malformed
        assert isinstance(parsed, dict)

    def test_envelope_has_exact_expected_shape(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The empty envelope is a *contract*: two keys, empty
        # ``blueprints`` array, versioned envelope. Asserting equality
        # (not subset) means an accidental extra top-level key fails
        # here, before it surprises a consumer.
        _exit, out, _err = _run_list([], json_mode=True, capsys=capsys)
        parsed = json.loads(out)
        assert parsed == {
            "blueprints": [],
            "schema_version": "1.0.0",
        }

    def test_stdout_has_no_human_framing(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Belt-and-braces: even if a future change adds a helpful
        # human-readable message alongside the JSON, this test fails.
        # The rule is "stdout is the payload, nothing else."
        _exit, out, _err = _run_list([], json_mode=True, capsys=capsys)
        assert "No blueprints" not in out
        assert "✓" not in out
        assert "⚠" not in out

    def test_stderr_is_empty(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # An empty catalogue is not a diagnostic event for machine
        # consumers — it is the answer to the question they asked.
        # Emitting a warning here would cause `forge list --json | jq`
        # pipelines to display a warning that the consumer cannot act
        # on (and that a shell may interpret as an error signal).
        _exit, _out, err = _run_list([], json_mode=True, capsys=capsys)
        assert err == "", f"Empty catalogue in JSON mode must not emit a warning; " f"got: {err!r}"

    def test_no_trailing_newlines_beyond_emit_contract(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `ui.emit` appends exactly one newline. Not zero (a shell
        # prompt glued to the payload), not two (a blank line that
        # breaks `wc -l` expectations).
        _exit, out, _err = _run_list([], json_mode=True, capsys=capsys)
        assert out.endswith("\n")
        assert not out.endswith("\n\n")


# ---------------------------------------------------------------------------
# Filtered-to-empty vs. genuinely empty -- the recovery distinction
# ---------------------------------------------------------------------------


class TestFilteredToEmpty:
    """A populated catalogue that filters narrow to nothing.

    This is *not* the same failure as an empty catalogue, and the
    message must be different because the recovery is different:

    * Empty catalogue → the install is broken or minimal; the user
      needs to install blueprints or check `forge doctor`.
    * Filtered to empty → the install is fine; the user picked a
      filter that matches nothing and needs to widen it.

    Using one message for both would send the user down the wrong
    recovery path, which is a small thing but the kind of small thing
    that makes a CLI frustrating to use over time.
    """

    @pytest.fixture()
    def populated(
        self,
    ) -> list[tuple[DiscoveredBlueprint, BlueprintStatus]]:
        """Three blueprints across two categories and one source.

        Enough variety to filter to non-empty on some criteria and to
        empty on others, without depending on the actual shipped
        catalogue.
        """
        return [
            (_discovered("a.a", "A", category=Category.api), BlueprintStatus.ok),
            (_discovered("b.b", "B", category=Category.api), BlueprintStatus.ok),
            (_discovered("c.c", "C", category=Category.frontend), BlueprintStatus.ok),
        ]

    def test_filter_to_empty_exits_zero(
        self,
        populated: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Filtering by a category nothing matches is a valid query
        # with an empty answer.
        exit_code, _out, _err = _run_list(
            populated,
            category="library",
            capsys=capsys,
        )
        assert exit_code == 0

    def test_filter_to_empty_warns_on_stderr(
        self,
        populated: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, _out, err = _run_list(
            populated,
            category="library",
            capsys=capsys,
        )
        assert err != ""

    def test_filter_to_empty_message_describes_filters_not_install(
        self,
        populated: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The message must point at filters, not at the installation.
        # A user who typed a filter that matches nothing is helped by
        # "no blueprints match the given filters"; they are *misled*
        # by "check your installation", which sends them to
        # `forge doctor` for a problem that is not there.
        _exit, _out, err = _run_list(
            populated,
            category="library",
            capsys=capsys,
        )
        assert (
            "filter" in err.lower()
        ), f"Filter-narrowed-to-empty must mention filters; got: {err!r}"
        assert "installation" not in err.lower(), (
            f"Filter-narrowed-to-empty must not imply a broken " f"installation; got: {err!r}"
        )

    def test_filter_to_empty_in_json_mode_emits_empty_envelope(
        self,
        populated: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # In JSON mode, the distinction between "empty" and
        # "filtered to empty" collapses: both are the empty envelope,
        # because a machine consumer asked for a payload and got one.
        # The distinction is a *human-facing* nicety.
        _exit, out, _err = _run_list(
            populated,
            json_mode=True,
            category="library",
            capsys=capsys,
        )
        parsed = json.loads(out)
        assert parsed == {
            "blueprints": [],
            "schema_version": "1.0.0",
        }


# ---------------------------------------------------------------------------
# Snapshot fixtures -- the empty envelope is a reviewed artefact
# ---------------------------------------------------------------------------


class TestEmptyEnvelopeSnapshot:
    """The empty envelope is checked into `tests/fixtures/list/` as a
    golden fixture.

    `test_list_json.py::TestSnapshots` also exercises this fixture from
    the JSON side; the duplication here is intentional and small. A
    change that touches the empty-envelope path can break the fixture
    from *either* side, and having both catch it (one from "the
    command's empty behaviour", one from "the JSON contract") shortens
    the debug loop when something goes wrong.

    Regenerate with:
        UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/cli/test_list_empty.py --no-cov
    """

    def _assert_golden(self, actual: str, fixture_name: str) -> None:
        fixture_path = _FIXTURES / fixture_name

        if os.environ.get("UPDATE_SNAPSHOTS") == "1":
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            fixture_path.write_text(actual, encoding="utf-8")
            return

        if not fixture_path.exists():
            pytest.fail(
                f"Golden fixture {fixture_name!r} does not exist.\n"
                f"Regenerate it with:\n"
                f"    UPDATE_SNAPSHOTS=1 uv run pytest "
                f"{Path(__file__).name} --no-cov\n"
                f"and inspect the diff before committing."
            )

        expected = fixture_path.read_text(encoding="utf-8")
        assert actual == expected, (
            f"\nGolden fixture mismatch for {fixture_name}.\n"
            f"The empty envelope is a *contract* for `forge list "
            f"--json`; any change is consumer-visible.\n"
            f"Regenerate with:\n"
            f"    UPDATE_SNAPSHOTS=1 uv run pytest "
            f"{Path(__file__).name} --no-cov\n"
            f"and update CLI_LIST_OUTPUT.md if the shape changed.\n\n"
            f"--- expected ---\n{expected}\n"
            f"--- actual ---\n{actual}\n"
        )

    def test_empty_envelope_matches_golden_fixture(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _exit, out, _err = _run_list([], json_mode=True, capsys=capsys)
        # `ui.emit` appends one newline; the fixture captures the
        # payload without it (the newline is `emit`'s framing, not the
        # payload's).
        self._assert_golden(out.rstrip("\n"), "envelope_empty.json")


# `os` is imported here rather than at module scope to keep the
# top-of-file imports focused on what most tests need. The snapshot
# branch is the only consumer, and it is at the bottom of the file.
import os  # noqa: E402
