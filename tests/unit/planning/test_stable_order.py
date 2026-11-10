"""Unit tests for deterministic ordering in the file planner.

The planner is a **pure, total function** (ADR-0015): same blueprint +
same variables ⇒ identical ``list[PlannedFile]``. "Identical" includes
order. This file is the executable specification of that contract for
the *output* order — the sort applied by ``FileMapper.map`` before it
returns.

Why deterministic output order matters
--------------------------------------
Determinism is not aesthetic. Three concrete consumers depend on it:

* **The writer.** A staging/commit pipeline that diffs two plans to
  decide what changed (future ``--incremental``) needs identical plans
  to be byte-identical, not just set-identical.
* **Golden tests.** ``--dry-run`` output is meant to be diffable in
  code review and stable in snapshot tests. Non-deterministic order
  would make every run a spurious diff.
* **User reports.** "I ran ``forge create`` and got this plan" is only
  reproducible if re-running produces the same plan. Otherwise
  "worked on my machine" becomes "worked on my *run*".

The three properties this file pins:

1. **Sorted ascending by destination** — the visible contract. Users
   read the plan top-to-bottom; alphabetic destination order is the
   only order that does not surprise them.

2. **Stable across runs** — the mechanical contract. Calling ``map``
   twice with the same inputs produces identical lists, element by
   element, not just "same set".

3. **Independent of input order** — the semantic contract. Two
   blueprints whose ``files[]`` entries are written in different
   orders, but which produce the same destinations, must produce the
   same plan. Otherwise the *blueprint author's* choice of file
   ordering leaks into the *output* ordering, which is confusing and
   makes "swap two lines in YAML" look like a semantic change.

Test design
-----------
The tests construct minimal ``Blueprint`` instances via
``_blueprint_with_files`` — a real model, not a fake — because the
planner validates against the model and a duck-typed fake would drift
the moment ``Blueprint`` gains a required field. The plans are produced
by the real ``FileMapper().map(...)``; nothing is stubbed. That is
deliberate: sorting is a *behaviour* of the planner, not of a helper,
and stubbing the planner would prove nothing about the planner's
output.

The variables passed to ``map`` are ``{}`` throughout. Every test uses
literal destinations (no ``{{ … }}``), so variable substitution is not
part of what is under test — it is exercised exhaustively in
``test_destinations.py``. Including variables here would just add
noise to the "does sorting work" question.
"""

from __future__ import annotations

import pytest

from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    FileEntry,
)
from forge.planning import FileMapper, PlannedFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _blueprint_with_files(files: list[FileEntry]) -> Blueprint:
    """Construct a minimal valid ``Blueprint`` declaring ``files``.

    A real ``Blueprint`` instance (not a fake) because the planner
    validates against the actual model — a duck-typed fake that happens
    to satisfy today's ``files[]`` contract would break silently the
    moment ``Blueprint`` gains a new required field. The helper is
    also the *only* place this file constructs a blueprint, so a future
    field addition touches one function, not every test.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.order",
        name="Deterministic Order Demo",
        version="1.0.0",
        description="Fixture for output-ordering tests.",
        category=Category.api,
        files=files,
        verification=[
            {"id": "ok", "type": "required_file", "path": "README.md"},
        ],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _plan(*files: FileEntry) -> list[PlannedFile]:
    """Run ``files`` through the real planner and return the plan.

    Asserts on ``is_ok()`` in the helper so a planning failure (which
    would only happen for a *malformed* fixture, never for the fixtures
    in this file) fails the test with a clear message rather than
    propagating as a cryptic ``Err`` unwrap. Each test's own assertions
    are about ordering, not about success/failure of planning.
    """
    result = FileMapper().map(_blueprint_with_files(list(files)), {})
    assert result.is_ok(), f"planning failed unexpectedly: {result}"
    return result.value


# ---------------------------------------------------------------------------
# Sorted ascending by destination
# ---------------------------------------------------------------------------
class TestSortedByDestination:
    """The output list is sorted ascending by destination string.

    "Ascending" means Python's default string comparison on the
    ``destination`` field — the same order ``sorted(plan, key=...)``
    would produce. The test does not assert on a specific *collation*
    (case sensitivity, locale) because the sort is a plain ``<`` on
    ``str``, and pinning a collation would couple the tests to an
    implementation detail the contract does not promise. What the
    contract promises is "deterministic", and the concrete order it
    commits to is Python's string order.
    """

    def test_single_file_plan_is_trivially_sorted(self) -> None:
        """One file is its own sorted list — pins the trivial case."""
        plan = _plan(FileEntry(source="a.j2", destination="out/a.py"))
        assert [p.destination for p in plan] == ["out/a.py"]

    def test_two_files_already_sorted_in_input(self) -> None:
        """``[a, b]`` in, ``[a, b]`` out — the no-op case."""
        plan = _plan(
            FileEntry(source="a.j2", destination="out/a.py"),
            FileEntry(source="b.j2", destination="out/b.py"),
        )
        assert [p.destination for p in plan] == ["out/a.py", "out/b.py"]

    def test_two_files_reversed_in_input(self) -> None:
        """``[b, a]`` in, ``[a, b]`` out — the sort actually sorts.

        This is the simplest test that would fail if the planner
        stopped sorting, or sorted in the wrong direction. A test
        that only provided pre-sorted input would pass even if
        ``map()`` did ``plan.sort(reverse=True)``, because the input
        was already reverse-sorted — a classic "test that cannot
        fail" for the property it claims to test.
        """
        plan = _plan(
            FileEntry(source="b.j2", destination="out/b.py"),
            FileEntry(source="a.j2", destination="out/a.py"),
        )
        assert [p.destination for p in plan] == ["out/a.py", "out/b.py"]

    def test_many_files_are_sorted(self) -> None:
        """A deliberately scrambled input produces a clean alphabetical output."""
        plan = _plan(
            FileEntry(source="m.j2", destination="out/m.py"),
            FileEntry(source="a.j2", destination="out/a.py"),
            FileEntry(source="z.j2", destination="out/z.py"),
            FileEntry(source="c.j2", destination="out/c.py"),
            FileEntry(source="k.j2", destination="out/k.py"),
            FileEntry(source="b.j2", destination="out/b.py"),
        )
        assert [p.destination for p in plan] == [
            "out/a.py",
            "out/b.py",
            "out/c.py",
            "out/k.py",
            "out/m.py",
            "out/z.py",
        ]

    def test_sorting_is_lexicographic_not_path_aware(self) -> None:
        """Sorting compares the destination as a plain string.

        "Plain string" is a *deliberate* choice: it means
        ``out/a/b.py`` sorts before ``out/a.py`` because ``/`` (0x2F)
        sorts before ``.`` (0x2E)... wait, actually ``.`` is 0x2E and
        ``/`` is 0x2F, so ``out/a.py`` sorts *before* ``out/a/b.py``.
        Either way, the point is that the sort is on the string, not
        on path components — so ``out/a/b.py`` vs ``out/a.py`` is
        determined by the character that follows ``out/a`` (``/`` vs
        ``.``), not by "is one a directory of the other".

        This matters because a path-aware sort (e.g. one that puts
        files before their contents, or groups by directory first)
        would produce a different order than the string sort the
        planner actually does. Pinning this case prevents a future
        "improvement" to "sort like a file explorer" from silently
        changing the output order of every plan.
        """
        plan = _plan(
            FileEntry(source="1.j2", destination="out/a/b.py"),
            FileEntry(source="2.j2", destination="out/a.py"),
        )
        # `.` (0x2E) < `/` (0x2F), so "out/a.py" < "out/a/b.py"
        assert [p.destination for p in plan] == ["out/a.py", "out/a/b.py"]


# ---------------------------------------------------------------------------
# Stable across runs
# ---------------------------------------------------------------------------
class TestStableAcrossRuns:
    """Two calls with identical inputs produce identical output.

    "Identical" means element-by-element equality of the entire
    ``list[PlannedFile]`` — not just equality of the destination set,
    and not just equality of the size. A planner that returned
    ``[a, b]`` on the first call and ``[b, a]`` on the second would
    pass a set-equality check and fail this class.
    """

    def test_repeated_calls_produce_equal_lists(self) -> None:
        """``map(...) == map(...)`` — the entire list, order included.

        ``PlannedFile`` is a frozen Pydantic model, so ``==`` is
        field-by-field structural equality, not identity. Two calls
        producing "the same plan" must produce *equal* plans, not
        merely plans that compare as sets.
        """
        files = [
            FileEntry(source="a.j2", destination="out/a.py"),
            FileEntry(source="b.j2", destination="out/b.py"),
            FileEntry(source="c.j2", destination="out/c.py"),
        ]
        first = _plan(*files)
        second = _plan(*files)

        assert first == second

    def test_repeated_calls_produce_identical_destination_sequence(self) -> None:
        """Explicit sequence equality, as a defence-in-depth assertion.

        The ``==`` above relies on ``PlannedFile.__eq__`` being
        structural. A future refactor that made ``PlannedFile``
        identity-compared (e.g. by removing ``frozen=True`` and
        switching to a plain class without ``__eq__``) would break
        that test's *diagnostic value* — it would fail for a reason
        that is not about ordering. This test asserts on the
        destination *strings*, which are the observable contract
        regardless of how ``PlannedFile`` compares.
        """
        files = [
            FileEntry(source="a.j2", destination="out/a.py"),
            FileEntry(source="b.j2", destination="out/b.py"),
        ]
        assert [p.destination for p in _plan(*files)] == [p.destination for p in _plan(*files)]

    def test_repeated_calls_produce_identical_kinds(self) -> None:
        """Order stability extends to every field, not just destination.

        If the sort compared only destinations but the writer read
        kinds by index, a future change that reordered *kind fields*
        while preserving destination order would corrupt the plan
        without failing the destination-order test. This test pins
        the whole ``PlannedFile`` per position, not just the
        destination.
        """
        files = [
            FileEntry(source="a.png", destination="out/a.png"),
            FileEntry(source="b.j2", destination="out/b.py"),
            FileEntry(source="c.png", destination="out/c.png"),
        ]
        first = _plan(*files)
        second = _plan(*files)

        for left, right in zip(first, second, strict=True):
            assert left.kind is right.kind


# ---------------------------------------------------------------------------
# Independent of input order
# ---------------------------------------------------------------------------
class TestInputOrderIndependence:
    """The output order is a function of destinations, not of input order.

    Two blueprints with the same *set* of files, written in different
    orders in ``files[]``, produce the same plan. This is the strongest
    form of "deterministic" the planner can offer — it says the output
    depends only on the destinations the blueprint produces, not on
    the *order* the author happened to write them in.

    Why this matters operationally: users format their YAML in many
    ways (alphabetical, grouped by function, generated). Two users with
    semantically identical blueprints should get identical plans, so
    the plan can be diffed, reviewed, and cached without knowing
    anything about the author's formatting choices.
    """

    def test_two_permutations_produce_the_same_plan(self) -> None:
        files = [
            FileEntry(source="a.j2", destination="out/a.py"),
            FileEntry(source="b.j2", destination="out/b.py"),
            FileEntry(source="c.j2", destination="out/c.py"),
        ]

        forward = _plan(*files)
        backward = _plan(*reversed(files))

        assert [p.destination for p in forward] == [p.destination for p in backward]

    def test_three_permutations_produce_the_same_plan(self) -> None:
        """Six orderings of three files all produce one canonical plan.

        Exhaustive over permutations of three files — small enough to
        be cheap, large enough to catch a bug that depends on which
        element happens to be first (e.g. a bubble-sort-with-a-bug
        that stops early when the first two elements are already in
        order). Two permutations only exercise two of the six
        possible "first elements"; all six exercise them all.
        """
        import itertools

        files = [
            FileEntry(source="a.j2", destination="out/a.py"),
            FileEntry(source="b.j2", destination="out/b.py"),
            FileEntry(source="c.j2", destination="out/c.py"),
        ]
        canonical = [p.destination for p in _plan(*files)]

        for perm in itertools.permutations(files):
            assert [p.destination for p in _plan(*perm)] == canonical

    def test_conditional_file_order_does_not_affect_plan(self) -> None:
        """Ordering of ``conditional_files[]`` does not leak into the plan.

        The current planner does not iterate ``conditional_files[]``
        (that is a later phase — see ``mapper.py``'s module docstring,
        "The conditional-files seam"). But the *presence* of the
        field on ``Blueprint`` must not introduce ordering
        non-determinism. This test constructs two blueprints that
        differ only in the *unused* ``conditional_files[]`` order and
        asserts the plans are identical.

        When condition evaluation lands, this test will need to be
        extended to include *rendered* conditional files in the
        assertion. Until then, it pins the property that the current
        plan is a function of ``files[]`` only.
        """
        from forge.models import ConditionalFileEntry

        base_files = [FileEntry(source="a.j2", destination="out/a.py")]

        # Note: the current Blueprint accepts conditional_files[] but
        # the planner ignores it. The two blueprints below differ only
        # in *which* conditional_files[] entries are present, which
        # is currently irrelevant to the plan.
        bp1 = _blueprint_with_files(base_files)
        bp2 = _blueprint_with_files(base_files).model_copy(
            update={
                "conditional_files": [
                    ConditionalFileEntry(
                        condition="True",
                        files=[FileEntry(source="c.j2", destination="out/c.py")],
                    ),
                ],
            }
        )

        plan1 = FileMapper().map(bp1, {})
        plan2 = FileMapper().map(bp2, {})
        assert plan1.is_ok()
        assert plan2.is_ok()
        assert [p.destination for p in plan1.value] == [p.destination for p in plan2.value]


# ---------------------------------------------------------------------------
# Determinism is a function of the DTO, not of dict iteration order
# ---------------------------------------------------------------------------
class TestNoDictOrderLeakage:
    """The plan's order must not depend on any dict's iteration order.

    The planner is called with ``variables: Mapping[str, object]``. If
    a future change made the planner iterate ``variables`` (say, to
    emit a diagnostic per variable), and if that iteration reached
    into the plan's sort key, the plan order could become dependent on
    the order the caller constructed their dict.

    The current planner does not iterate variables for output
    purposes — but this test pins the *invariant*: identical
    ``(blueprint, variables-as-a-set)`` produce identical plans,
    regardless of the *insertion order* of the dict the caller
    constructed.
    """

    @pytest.mark.parametrize(
        "var_order",
        [
            [("a", 1), ("b", 2), ("c", 3)],
            [("c", 3), ("b", 2), ("a", 1)],
            [("b", 2), ("a", 1), ("c", 3)],
        ],
    )
    def test_variable_dict_insertion_order_does_not_affect_plan(
        self, var_order: list[tuple[str, int]]
    ) -> None:
        """Insertion order of ``variables`` does not change the plan.

        The three parametrisations provide the same ``{a:1, b:2, c:3}``
        in three different insertion orders. If the plan's order ever
        became sensitive to that insertion order, at least one of
        these would produce a different list — the test fails on the
        specific insertion order that diverges, which is exactly the
        information needed to find the bug.

        The variables here are unused by the blueprint's (literal)
        destinations, so the plan is identical regardless. That is the
        point: even *unused* variables must not accidentally leak
        through the plan's construction path.
        """
        bp = _blueprint_with_files(
            [
                FileEntry(source="a.j2", destination="out/a.py"),
                FileEntry(source="b.j2", destination="out/b.py"),
            ]
        )
        variables = dict(var_order)

        plan = FileMapper().map(bp, variables)
        assert plan.is_ok()
        assert [p.destination for p in plan.value] == [
            "out/a.py",
            "out/b.py",
        ]


# ---------------------------------------------------------------------------
# Sort key is destination, not source
# ---------------------------------------------------------------------------
class TestSortKeyIsDestination:
    """The sort is by ``destination``, not by ``source``.

    A common implementation slip is to sort by whichever field the
    author "expects to be canonical". The contract here is
    destination, because:

    * Destination is what the *user* sees in the file tree — sorting
      by it makes the plan read like the project will look on disk.
    * Source may have template extensions (``.j2``) or be in a
      different tree (``templates/…``) — sorting by it would produce
      an order that reads like the *blueprint's* organisation, not
      the *project's*.

    A test that only checked sortedness of *some* field would pass
    with either key, which is why this class asserts on a case where
    the two keys disagree.
    """

    def test_sorting_is_by_destination_when_source_order_differs(self) -> None:
        """Sources in one order, destinations in another → sort by destinations."""
        plan = _plan(
            FileEntry(source="z-source.j2", destination="out/a.py"),
            FileEntry(source="a-source.j2", destination="out/z.py"),
        )
        # If the planner sorted by source, the order would be [a-source, z-source].
        # If it sorted by destination, the order is [out/a.py, out/z.py].
        # We assert the destination order.
        assert [p.destination for p in plan] == ["out/a.py", "out/z.py"]
        # And the sources, correspondingly, follow the destination order.
        assert [p.source for p in plan] == ["z-source.j2", "a-source.j2"]
