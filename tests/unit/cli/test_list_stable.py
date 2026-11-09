"""Unit tests for `forge list` catalogue ordering.

What this file covers
---------------------
The command's obligation to produce a **stable, sorted output** — by
``id`` ascending — regardless of what order the discovery service
returns candidates in. This applies to both output modes:

* table mode (the header row is not part of the ordering, but the
  body rows are);
* JSON mode (the ``blueprints`` array is the *contract* per
  ``CLI_LIST_OUTPUT.md`` — consumers may rely on ``id``-ascending
  order for the current ``schema_version``).

What this file deliberately does NOT cover
------------------------------------------
* **Renderer ordering** — both renderers preserve input order
  verbatim (per ADR-0012). That property is tested in
  ``test_list_table.py::TestRenderTableOrdering`` and
  ``test_list_json.py`` (via byte-identical output for the same
  input). Testing it again here would confuse "who sorts?" — the
  answer is the command, once, before building the DTO.
* **Discovery service ordering** — the resolver already produces a
  sorted-by-id catalogue (see ``discovery/resolver.py::to_metadata``).
  But the *command* must not rely on that, because (a) the resolver's
  ordering is an implementation detail that could change, and (b) the
  CLI's output shape is a public contract that must not silently
  reshuffle. This file asserts the command's *own* sort.
* **Case sensitivity, locale, or non-ASCII ids** — blueprint ids are
  ASCII lowercase by schema (``^[a-z][a-z0-9]*(\\.[a-z][a-z0-9]*)+$``),
  so the schema itself rules out the cases that would make sorting
  locale-dependent. Asserting on those cases here would test
  Python's ``str`` sort, not the command.

Why "stability" is a separate concern
-------------------------------------
"Stable" here means two things, both of which matter and both of
which are tested:

1. **Deterministic**: given the same input, produce the same output.
   A catalogue whose order flips between runs breaks snapshot tests,
   ``diff``-based review, and ``git``-tracked golden files.

2. **Sorted by contract**: the order is *defined*, not merely
   consistent. A catalogue consistently sorted by "whatever order
   the filesystem returned" is deterministic but wrong; a consumer
   that relies on ``id``-ascending order (documented in
   ``CLI_LIST_OUTPUT.md``) needs the *defined* order, not just any
   consistent one.

Both properties are tested separately, because they fail for
different reasons and need different fixes:

* A *determinism* failure means something is reading external state
  (a set without sorting, a directory iterator, an unordered
  collection) and the fix is to sort or freeze the input.
* A *sortedness* failure means the sort key or direction is wrong,
  and the fix is to change the sort call.

A note on test input, learned the hard way
------------------------------------------
Three tests in this file originally used input that was *already*
in id-sorted order. Those tests passed whether the sort existed or
not — they could not distinguish "sort works" from "sort is absent".
A test for "X sorts correctly" must supply input where a broken X
would produce a detectably different output. All three inputs have
been changed to satisfy that rule, and the reason is documented on
each one. When adding a new ordering test, apply the same rule:

    If the input is already sorted, the test is a *no-op* that
    happens to run. Reorder the input so the sort has work to do.
"""

from __future__ import annotations

import json
import random
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
# Helpers
# ---------------------------------------------------------------------------


def _discovered(
    id_: str,
    name: str | None = None,
    *,
    version: str = "1.0.0",
    category: Category = Category.api,
    source: BlueprintSource = BlueprintSource.OFFICIAL,
) -> DiscoveredBlueprint:
    """Build a `DiscoveredBlueprint` with a minimum of ceremony.

    The name defaults to the id so a test that only cares about
    ordering reads clearly: ``_discovered("a.a")`` produces a row
    whose name happens to equal its id, which is fine for ordering
    purposes and makes the resulting JSON easier to eyeball.

    The first parameter is ``id_`` (with the trailing underscore) for
    two reasons: ``id`` is a Python builtin that shadowing it would
    hide, and the underscore signals "this is the blueprint id, not
    the builtin".
    """
    return DiscoveredBlueprint(
        id=id_,
        name=name if name is not None else id_,
        version=version,
        description="d",
        category=category,
        source=source,
    )


def _run_list(
    pairs: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
    *,
    json_mode: bool = False,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """Drive `forge list`, capturing whatever it emits on stdout.

    Returns the stdout only — ordering tests do not care about the
    exit code (it is always 0 on a populated catalogue) or stderr
    (empty in the happy path). Returning one thing keeps each test
    assertion focused.
    """
    with patch(
        "forge.cli.commands.list.BlueprintDiscoveryService",
    ) as service_cls:
        service_cls.return_value.discover_with_status.return_value = pairs
        exit_code = list_cmd.run(SimpleNamespace(json=json_mode, category="all", source="all"))
    # Populated catalogue always succeeds; asserting here rather than
    # in every test keeps the ordering tests from repeating the same
    # boilerplate.
    assert exit_code == 0
    return capsys.readouterr().out


def _ids_in_json_order(stdout: str) -> list[str]:
    """Extract the blueprint ids from a `--json` payload, in order."""
    return [item["id"] for item in json.loads(stdout)["blueprints"]]


def _ids_in_table_order(stdout: str) -> list[str]:
    """Extract the blueprint ids from a table payload, in order.

    Skips the header (line 0) and separator (line 1); the first
    whitespace-delimited token on each remaining line is the id.
    Truncated ids (with the ellipsis glyph) are *not* handled here
    because ordering tests never supply a value above the column
    cap — if they did, that would be testing truncation, not
    ordering.
    """
    body = stdout.splitlines()[2:]
    return [line.split()[0] for line in body if line.strip()]


# ---------------------------------------------------------------------------
# Sortedness — the *contract*
# ---------------------------------------------------------------------------


class TestSortedById:
    """The catalogue is sorted by id ascending, in both modes.

    These tests do not randomise their input — they feed a specific,
    known-out-of-order list and assert the specific, known-in-order
    output. A test that shuffles and then only checks "is sorted"
    would pass even if the command had a no-op sort (because a
    pre-sorted shuffle is sorted), so the specific inputs here are
    load-bearing.
    """

    def test_table_rows_are_sorted_by_id(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Input: z, a, m — reverse-lexicographic-and-then-some, so any
        # pass-through (no sort) or wrong-direction sort is caught
        # immediately.
        pairs = [
            (_discovered("z.z"), BlueprintStatus.ok),
            (_discovered("a.a"), BlueprintStatus.ok),
            (_discovered("m.m"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, capsys=capsys)
        assert _ids_in_table_order(out) == ["a.a", "m.m", "z.z"]

    def test_json_rows_are_sorted_by_id(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pairs = [
            (_discovered("z.z"), BlueprintStatus.ok),
            (_discovered("a.a"), BlueprintStatus.ok),
            (_discovered("m.m"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == ["a.a", "m.m", "z.z"]

    def test_sorted_regardless_of_input_permutation(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Every permutation of three ids must produce the same sorted
        # output. A command that sorted *once* (e.g. cached a sorted
        # list on first call and reused it) would pass the tests
        # above but fail here on subsequent permutations, because the
        # mock returns a new list each call.
        #
        # Note: the comprehension uses ``id_`` (not ``id``) as its
        # loop variable, matching the helper's parameter name. A
        # previous version of this file used ``id`` here, which
        # raised ``NameError: name 'id_' is not defined`` because the
        # helper was called with ``id_`` inside a comprehension whose
        # only bound name was ``id``. The rename is the fix.
        import itertools

        ids = ["a.a", "m.m", "z.z"]
        expected = ["a.a", "m.m", "z.z"]
        for perm in itertools.permutations(ids):
            pairs = [(_discovered(id_), BlueprintStatus.ok) for id_ in perm]
            out = _run_list(pairs, json_mode=True, capsys=capsys)
            assert _ids_in_json_order(out) == expected, (
                f"permutation {perm} produced order "
                f"{_ids_in_json_order(out)}; expected {expected}"
            )

    def test_sorted_lexicographically_not_numerically(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A common bug is to sort by *something else* that happens to
        # match lexicographic order for simple inputs (e.g. by name,
        # by length, by hash, by a numeric suffix that was misread as
        # the id). This test uses ids that sort one way by id and the
        # opposite way by *name*, so a sort by name is caught.
        #
        # Input deliberately descrambled:
        #   given:  c.c  b.b  a.a   (name x / y / z)
        #   by id:  a.a  b.b  c.c   (correct)
        #   by name: c.c  b.b  a.a  (x, y, z ascending -> wrong)
        #
        # The input order (c, b, a) is already the *wrong* order for
        # an id-sort and the *right* order for a name-sort. So a
        # no-op sort (pass-through) and a name-sort both produce
        # ['c.c', 'b.b', 'a.a'] — the test fails. Only the correct
        # id-sort produces ['a.a', 'b.b', 'c.c'] and passes.
        #
        # A previous version of this test fed (a, b, c) — already
        # sorted by id — which passed even without a sort. The
        # reordering here is the fix.
        pairs = [
            (_discovered("c.c", "x"), BlueprintStatus.ok),
            (_discovered("b.b", "y"), BlueprintStatus.ok),
            (_discovered("a.a", "z"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == ["a.a", "b.b", "c.c"], (
            "catalogue is sorted by name, not by id — the sort key is "
            "wrong (see ADR-0012: 'sorted by id')"
        )

    def test_sorted_ascending_not_descending(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The direction is part of the contract. Ascending, not
        # descending. This is trivially broken by a ``reverse=True``
        # sneaking into a refactor.
        #
        # Input deliberately descending, so:
        #   no sort     -> ['c.c', 'b.b', 'a.a']  FAIL
        #   wrong dir   -> ['c.c', 'b.b', 'a.a']  FAIL
        #   correct     -> ['a.a', 'b.b', 'c.c']  PASS
        #
        # A previous version fed (a, b, c) — already ascending — which
        # passed even without a sort. The reordering here is the fix.
        pairs = [
            (_discovered("c.c"), BlueprintStatus.ok),
            (_discovered("b.b"), BlueprintStatus.ok),
            (_discovered("a.a"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == ["a.a", "b.b", "c.c"]


# ---------------------------------------------------------------------------
# Determinism — the *other half* of stability
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same input, same output, every run.

    Determinism is tested independently of sortedness because they
    fail for different reasons and need different fixes. A command
    that consistently produces an *unsorted but deterministic* order
    is a sortedness bug; a command that consistently produces a
    *sorted but non-deterministic* order (e.g. it sorts by a key
    with ties) is a determinism bug.

    Note that within a single Python process, a well-behaved pure
    function like `run` will naturally produce identical output for
    identical input, because there is no hidden state. These tests
    are largely *regression guards*: they ensure a future change
    does not introduce a source of nondeterminism (a set, a random
    tiebreaker, a filesystem iteration), which would pass sortedness
    tests but break byte-level fixtures in
    ``tests/fixtures/list/``.
    """

    def test_repeated_runs_produce_identical_bytes(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Byte-level equality, not just "same sorted order": the
        # golden fixtures under ``tests/fixtures/list/`` depend on
        # exact bytes. A future change that emits the same order but
        # a different representation (extra spacing, different
        # quoting) would break those fixtures, so this test asserts
        # the strictest version of determinism.
        pairs = [
            (_discovered("c.c"), BlueprintStatus.ok),
            (_discovered("a.a"), BlueprintStatus.ok),
            (_discovered("b.b"), BlueprintStatus.ok),
        ]
        out1 = _run_list(pairs, json_mode=True, capsys=capsys)
        out2 = _run_list(pairs, json_mode=True, capsys=capsys)
        out3 = _run_list(pairs, json_mode=True, capsys=capsys)
        assert out1 == out2 == out3

    def test_table_and_json_agree_on_order(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The two output modes are different *presentations* of the
        # same DTO. Their row order must agree — otherwise a user
        # switching between them (for human inspection vs. piping
        # to a tool) sees a different catalogue order, which is
        # confusing and, for the JSON side, a contract violation.
        pairs = [
            (_discovered("z.z"), BlueprintStatus.ok),
            (_discovered("a.a"), BlueprintStatus.ok),
            (_discovered("m.m"), BlueprintStatus.ok),
        ]
        table_ids = _ids_in_table_order(_run_list(pairs, capsys=capsys))
        json_ids = _ids_in_json_order(_run_list(pairs, json_mode=True, capsys=capsys))
        assert table_ids == json_ids, (
            "table mode and JSON mode disagree on row order; the "
            "command must sort once, before building the DTO (ADR-0012)"
        )

    @pytest.mark.parametrize("seed", [0, 1, 42, 999, 12345])
    def test_random_permutations_always_produce_same_order(
        self,
        seed: int,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Property-style test: for any random permutation of a fixed
        # set of ids, the output order is always the same. The fixed
        # seeds make the test reproducible; a nondeterminism bug that
        # only appears on some seeds would be caught by at least one
        # of them (in practice by all of them, because the seeds are
        # chosen to be spread out).
        #
        # Note: the comprehension uses ``id_`` (not ``id``) as its
        # loop variable, matching the helper's parameter name. See
        # ``test_sorted_regardless_of_input_permutation`` for the
        # rationale.
        #
        # ``random.Random`` (not ``secrets``) is the correct primitive
        # here: the purpose is reproducible permutation for a test, not
        # cryptographic unpredictability. S311's concern -- that a
        # pseudo-random generator is unsuitable for security-sensitive
        # use -- does not apply, because nothing security-relevant is
        # keyed off this value and the seed is a literal in the
        # parametrize list.
        rng = random.Random(seed)  # noqa: S311
        ids = ["e.blueprint", "b.blueprint", "d.blueprint", "a.blueprint", "c.blueprint"]
        rng.shuffle(ids)
        pairs = [(_discovered(id_), BlueprintStatus.ok) for id_ in ids]

        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == [
            "a.blueprint",
            "b.blueprint",
            "c.blueprint",
            "d.blueprint",
            "e.blueprint",
        ]


# ---------------------------------------------------------------------------
# Status does not affect ordering
# ---------------------------------------------------------------------------


class TestStatusDoesNotAffectOrdering:
    """Sort key is the id, not the status.

    A tempting "improvement" is to sort by status first (stale before
    ok, or vice versa) so users see problems at the top. That is a
    reasonable *product* decision, but it is not this command's
    contract today, and adopting it silently would break every
    consumer relying on id-sorted order. If the product wants
    status-aware ordering, it should be additive (a separate flag,
    or a `sort` column) and it should be decided in an ADR, not
    slipped into a refactor.

    The tests here fail if someone does slip it in.
    """

    def test_status_does_not_reorder_rows(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Deliberately interleaved statuses and non-alphabetical ids
        # so a "stale first" sort would produce a detectably different
        # order than an id-sort.
        #
        # Input:
        #   d.d  stale
        #   b.b  ok
        #   c.c  stale
        #   a.a  ok
        #
        # By id (correct):     a.a  b.b  c.c  d.d
        # By status then id:   b.b  a.a  d.d  c.c   (or similar; the
        #                     point is that the first row differs)
        #
        # So the two sorts produce detectably different outputs and
        # the assertion below is not vacuous.
        #
        # A previous version of this test used (a, b, c, d) with the
        # same interleaved statuses, which happened to already be id-
        # sorted; a status-sort would coincidentally produce a
        # different order for that input, so it did pass. The change
        # here is *belt-and-braces*: reordering the ids makes the
        # input unambiguously exercise the id-sort regardless of what
        # the status values happen to be.
        pairs = [
            (_discovered("d.d"), BlueprintStatus.stale),
            (_discovered("b.b"), BlueprintStatus.ok),
            (_discovered("c.c"), BlueprintStatus.stale),
            (_discovered("a.a"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == ["a.a", "b.b", "c.c", "d.d"], (
            "sorting appears to be status-aware; the contract is "
            "id-ascending, not status-then-id. If status-aware "
            "ordering is wanted, add it as an opt-in flag and update "
            "CLI_LIST_OUTPUT.md + ADR-0012."
        )


# ---------------------------------------------------------------------------
# Single-row and empty-catalogue edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Small edge cases of the sort, for completeness.

    Each is a single assertion, and each fails for a distinct reason
    if the sort implementation regresses.
    """

    def test_single_row_needs_no_sorting(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Trivial, but a sort implementation that crashes on a
        # single-element list (e.g. `sorted_[0]` indexing outside the
        # list) would be caught here rather than in production.
        pairs = [(_discovered("only.one"), BlueprintStatus.ok)]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == ["only.one"]

    def test_two_rows_sort_correctly(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The smallest non-trivial sort. Broken sort implementations
        # (off-by-one, reverse comparison, unstable ties) tend to
        # fail here first.
        pairs = [
            (_discovered("z.z"), BlueprintStatus.ok),
            (_discovered("a.a"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == ["a.a", "z.z"]

    def test_empty_catalogue_does_not_crash_sorting(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Sorting an empty list is legal in Python, but a sort
        # implementation that accesses ``sorted_[0]`` or ``min(...)``
        # without guarding would crash. The empty case is covered
        # more thoroughly in ``test_list_empty.py``; here the point is
        # only that the sort itself does not crash.
        pairs: list[tuple[DiscoveredBlueprint, BlueprintStatus]] = []
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == []


# ---------------------------------------------------------------------------
# Sortedness of a *realistic* catalogue
# ---------------------------------------------------------------------------


class TestRealisticCatalogue:
    """The sort behaves correctly on the shipped catalogue's shape.

    The synthetic tests above use one-character ids and hand-crafted
    permutations, which is enough to catch most bugs. This test uses
    the shipped official blueprint ids, which have a realistic
    length and dotted structure. Its purpose is to confirm that the
    sort works on the actual data users will see — a sort that
    accidentally compared by length or by the last dotted segment
    would pass the synthetic tests but fail here.
    """

    def test_official_blueprint_ids_sort_correctly(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Input deliberately out of alphabetical order.
        pairs = [
            (_discovered("typescript.frontend.react"), BlueprintStatus.ok),
            (_discovered("python.fastapi.api"), BlueprintStatus.ok),
            (_discovered("python.automation.cli"), BlueprintStatus.ok),
        ]
        out = _run_list(pairs, json_mode=True, capsys=capsys)
        assert _ids_in_json_order(out) == [
            "python.automation.cli",
            "python.fastapi.api",
            "typescript.frontend.react",
        ], (
            "official-catalogue ids do not sort as expected. This "
            "would happen if the sort key were (for example) the last "
            "segment of the id, or the id length."
        )
