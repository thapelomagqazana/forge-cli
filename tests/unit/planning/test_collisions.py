"""Unit tests for destination-collision detection.

The collision detector answers one question: *given a list of planned
files, which destinations are claimed by more than one source?* It is
the last line of defence before the writer creates files, and the
failure mode if it is wrong is severe:

* **A missed collision** means two sources write to the same path, and
  the second silently overwrites the first. The user gets a project
  that is missing a file they can see in their blueprint, with no error
  to explain why.
* **A false collision** (reporting two different files as colliding)
  blocks a legitimate blueprint for no reason and sends the author on
  a hunt for a bug that does not exist.

Both failure modes are silent without tests, which is why this module
has a dedicated test file rather than relying on end-to-end coverage.

The detector is also where the *normalisation* contract from
``planning.destinations`` is cashed in: two spellings of the same file
(``src/a.py``, ``./src/a.py``, ``src//a.py``) must bucket together, or
the "collision" is invisible. The tests below exercise that directly
because it is the sharpest edge — it is the reason ``normalise`` exists
as a separate function, and the reason the collision detector does not
compare raw strings.

Test design
-----------
Each test builds the minimum input that exercises one branch of
``find_collisions``. The tests do not go through ``FileMapper`` — the
planner's role is to *produce* the ``PlannedFile`` list, and the
collision detector's role is to *inspect* it. Testing the inspector
against constructed inputs isolates the branch under test; a test that
went through ``FileMapper`` would conflate the two, and a regression in
either would fail the same test with the same symptom, which is the
opposite of what a good test does.

The tests also do not go through ``resolve`` (the destination-path
resolver) — that is tested in ``test_destinations.py``. Collision
detection receives *already-resolved* destinations; the resolver's job
is to make sure they are safe and canonical, the detector's job is to
make sure no two of them are the same file. Two responsibilities, two
test files.

Normalisation test design
-------------------------
``TestNormalisation`` parametrises over ``(spelling_a, spelling_b,
expected_canonical)`` — the expected canonical form is a *literal*
argument, not derived from ``normalise(spelling_a)``. That is
deliberate:

* A test that computed ``normalise(spelling_a)`` for its expected
  value would silently pass if ``normalise`` itself regressed (both
  sides of the assertion would compute the same wrong value). The
  purpose of the parameter is to pin the *literal* canonical form for
  each input shape, so a regression in ``normalise`` fails the
  specific parameter that regressed.
* A reader scanning the parameter table sees the canonical form for
  every variant at a glance, without needing to run ``normalise``
  mentally. That is the property the test is asserting, and it should
  be visible in the test, not computed by it.

The assertion failure that surfaced this (a parameter with the wrong
``expected`` value copy-pasted from a shorter path variant) is exactly
what the literal-expectation discipline catches: the test says
``'src/nested/a.py' == 'src/a.py'`` and the reader immediately sees
which side is wrong without running anything.
"""

from __future__ import annotations

import pytest

from forge.planning import (
    Finding,
    PlannedFile,
    PlannedFileKind,
)
from forge.planning.collisions import find_collisions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _file(
    *,
    source: str,
    destination: str,
    kind: PlannedFileKind = PlannedFileKind.RENDER,
) -> PlannedFile:
    """Build a ``PlannedFile`` with sane defaults for the fields under test.

    ``kind`` defaults to ``RENDER`` because the collision detector does
    not care about kind — it groups by destination only. Providing a
    valid default means tests that are not about kind do not have to
    think about it, and a future test that *is* about kind (say, a
    binary and a template whose destinations collide — the collision is
    real regardless of kind) can pass it explicitly.

    The constructor is called with keyword arguments because
    ``PlannedFile`` is a Pydantic model with positional order that is
    not part of its contract; a future field reorder would break
    positional callers for no reason.
    """
    return PlannedFile(source=source, destination=destination, kind=kind)


# ---------------------------------------------------------------------------
# No collisions
# ---------------------------------------------------------------------------
class TestNoCollisions:
    """The detector returns an empty list when nothing collides."""

    def test_empty_input_returns_no_collisions(self) -> None:
        """No planned files → no collisions. Trivial, but pinned.

        A common bug in aggregation functions is returning ``None`` for
        an empty input instead of ``[]``. The caller iterates the
        result; ``None`` would raise ``TypeError: 'NoneType' object is
        not iterable`` at the moment the loader tries to add more
        findings to an already-empty list. Pinning ``[]`` (not just
        ``falsy``) catches that class of bug in a way an ``assert not
        result`` would not.
        """
        assert find_collisions([]) == []

    def test_single_file_returns_no_collisions(self) -> None:
        """One planned file cannot collide with anything."""
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/a.py"),
            ]
        )
        assert result == []

    def test_distinct_destinations_return_no_collisions(self) -> None:
        """Three files, three distinct destinations, no collision."""
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/a.py"),
                _file(source="b.j2", destination="out/b.py"),
                _file(source="c.j2", destination="out/c.py"),
            ]
        )
        assert result == []

    def test_same_source_different_destinations_is_not_a_collision(self) -> None:
        """One source rendering to two destinations is legal.

        A blueprint may legitimately want the same template rendered
        twice — for example, a generic ``__init__.py.j2`` that
        generates both ``pkg/__init__.py`` and ``pkg/sub/__init__.py``.
        The detector must not confuse "same source" with "collision";
        only *destinations* are compared.
        """
        result = find_collisions(
            [
                _file(source="init.py.j2", destination="pkg/__init__.py"),
                _file(source="init.py.j2", destination="pkg/sub/__init__.py"),
            ]
        )
        assert result == []


# ---------------------------------------------------------------------------
# Basic collisions
# ---------------------------------------------------------------------------
class TestBasicCollisions:
    """Two sources, one destination, one finding."""

    def test_two_files_with_identical_destinations_collide(self) -> None:
        """The canonical case: two sources, same destination."""
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/same.py"),
                _file(source="b.j2", destination="out/same.py"),
            ]
        )

        assert len(result) == 1
        finding = result[0]
        assert isinstance(finding, Finding)
        assert finding.code == "destination.collision"
        assert finding.destination == "out/same.py"
        assert set(finding.sources) == {"a.j2", "b.j2"}
        assert finding.hint  # KPI: every finding carries a hint

    def test_three_files_with_identical_destinations_one_finding(self) -> None:
        """Three-way collision → one finding, not two.

        The detector groups by destination, not by source pairs. Three
        sources claiming one destination is *one* problem to fix (two of
        them must be renamed/moved), not three (each pair is not
        independent — fixing any two still leaves the third colliding
        with one of them). Reporting N choose 2 findings for N sources
        would be noise that obscures the actual fix.
        """
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/same.py"),
                _file(source="b.j2", destination="out/same.py"),
                _file(source="c.j2", destination="out/same.py"),
            ]
        )

        assert len(result) == 1
        assert set(result[0].sources) == {"a.j2", "b.j2", "c.j2"}

    def test_sources_are_preserved_in_input_order(self) -> None:
        """The ``sources`` tuple preserves the order the planner emitted them.

        Why order matters: the planner emits files in blueprint order
        (the order the author wrote them in ``files[]``). When a
        collision is reported, the sources should read in the order the
        author sees them in their YAML — that is the order that makes
        the finding easy to interpret. Alphabetical sorting would make
        the report read differently from the blueprint, adding a small
        translation step to every debugging session.
        """
        result = find_collisions(
            [
                _file(source="z.j2", destination="out/x.py"),
                _file(source="a.j2", destination="out/x.py"),
                _file(source="m.j2", destination="out/x.py"),
            ]
        )

        assert result[0].sources == ("z.j2", "a.j2", "m.j2")


# ---------------------------------------------------------------------------
# Normalisation — the collision detector's sharpest edge
# ---------------------------------------------------------------------------
class TestNormalisation:
    """Different spellings of the same destination must collide.

    This class exists because it is the *only* class of bug that a
    straightforward "compare the strings" implementation misses. The
    planner emits destinations after running them through
    ``destinations.resolve``, which strips the leading ``./`` and
    normalises path separators — but the collision detector must not
    *assume* that. It runs ``normalise`` itself, so even a caller that
    hands it pre-normalised strings gets a correct answer, and a caller
    that hands it un-normalised strings does not slip past.

    If this class were missing, a blueprint with ``./src/a.py`` in one
    ``files[]`` entry and ``src/a.py`` in another would pass validation
    silently and then have one file overwrite the other at write time.
    That is the exact bug the detector exists to prevent, so it must be
    the most thoroughly-tested behaviour here, not an edge case
    mentioned in passing.

    The canonical form is passed *literally* as the third parameter,
    not derived from ``normalise(spelling_a)`` — see the module
    docstring's "Normalisation test design" section for why.
    """

    @pytest.mark.parametrize(
        ("spelling_a", "spelling_b", "expected_canonical"),
        [
            ("src/a.py", "./src/a.py", "src/a.py"),
            ("src/a.py", "src//a.py", "src/a.py"),
            ("src/a.py", "src\\a.py", "src/a.py"),
            ("src/a.py", "src/a.py/", "src/a.py"),
            ("src/nested/a.py", "src/nested//a.py", "src/nested/a.py"),
            ("./src/a.py", "src//a.py", "src/a.py"),
        ],
    )
    def test_equivalent_spellings_collide(
        self,
        spelling_a: str,
        spelling_b: str,
        expected_canonical: str,
    ) -> None:
        """Every spelling variant of ``src/a.py`` collides with itself."""
        result = find_collisions(
            [
                _file(source="a.j2", destination=spelling_a),
                _file(source="b.j2", destination=spelling_b),
            ]
        )

        assert len(result) == 1, (
            f"{spelling_a!r} and {spelling_b!r} resolve to the same "
            f"file but were not reported as colliding. The destination "
            f"would be silently overwritten."
        )
        assert result[0].destination == expected_canonical

    def test_finding_destination_is_the_normalised_form(self) -> None:
        """The reported destination is the canonical spelling, not the raw input.

        A user should see *one* spelling of the colliding file — the
        canonical one — so the fix ("rename one of these") is
        unambiguous about which file is meant. Reporting either of the
        raw spellings would make the finding read like a typo report
        ("``./src/a.py`` and ``src//a.py``") when the actual problem is
        that both name the same file.
        """
        result = find_collisions(
            [
                _file(source="a.j2", destination="./src/a.py"),
                _file(source="b.j2", destination="src//a.py"),
            ]
        )

        assert result[0].destination == "src/a.py"


# ---------------------------------------------------------------------------
# Multiple independent collisions
# ---------------------------------------------------------------------------
class TestMultipleCollisions:
    """Several unrelated collisions are reported together, sorted.

    The planner aggregates every problem it finds into one
    ``MappingError`` (ADR-0015), and the collision detector must
    support that: a blueprint with three collisions should produce
    three findings, not fail on the first. The user fixes all three in
    one editing pass rather than three round-trips through ``forge
    create``.
    """

    def test_two_independent_collisions_produce_two_findings(self) -> None:
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/x.py"),
                _file(source="b.j2", destination="out/x.py"),
                _file(source="c.j2", destination="out/y.py"),
                _file(source="d.j2", destination="out/y.py"),
            ]
        )

        assert len(result) == 2
        assert {f.destination for f in result} == {"out/x.py", "out/y.py"}

    def test_findings_are_sorted_by_destination(self) -> None:
        """Findings are sorted for deterministic reports.

        Determinism is a contract, not a nice-to-have: it makes the
        report diffable in CI, makes golden-snapshot tests stable, and
        makes user bug reports reproducible. Sorting at the source (in
        the detector) means every consumer gets determinism for free,
        rather than each consumer needing to sort before rendering.
        """
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/z.py"),
                _file(source="b.j2", destination="out/z.py"),
                _file(source="c.j2", destination="out/a.py"),
                _file(source="d.j2", destination="out/a.py"),
            ]
        )

        assert [f.destination for f in result] == ["out/a.py", "out/z.py"]

    def test_sorted_independent_of_input_order(self) -> None:
        """Two runs with different input orders produce identical reports."""
        forward = find_collisions(
            [
                _file(source="a.j2", destination="out/a.py"),
                _file(source="b.j2", destination="out/a.py"),
                _file(source="c.j2", destination="out/z.py"),
                _file(source="d.j2", destination="out/z.py"),
            ]
        )
        reverse = find_collisions(
            [
                _file(source="d.j2", destination="out/z.py"),
                _file(source="c.j2", destination="out/z.py"),
                _file(source="b.j2", destination="out/a.py"),
                _file(source="a.j2", destination="out/a.py"),
            ]
        )

        assert [f.destination for f in forward] == [f.destination for f in reverse]


# ---------------------------------------------------------------------------
# Finding invariants
# ---------------------------------------------------------------------------
class TestFindingInvariants:
    """Contract every collision finding must satisfy.

    These invariants are what makes collision findings renderable by
    the CLI's generic error renderer. A finding without a hint is a
    dead end (the user sees "collision" and no fix); a finding without
    a source list is unactionable (which two files?).
    """

    def test_collision_finding_has_code_and_hint(self) -> None:
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/x.py"),
                _file(source="b.j2", destination="out/x.py"),
            ]
        )

        finding = result[0]
        assert finding.code == "destination.collision"
        assert finding.hint, "every finding must carry a hint (traceability KPI)"

    def test_collision_finding_has_at_least_two_sources(self) -> None:
        """By definition a collision is at least two sources.

        The detector only constructs a finding when ``len(members) >
        1``. That invariant is load-bearing for the CLI: the rendering
        says "N sources map to the same destination" and lists them.
        A finding with one source would render "1 sources map to the
        same destination" — a sentence that is not just wrong but
        nonsensical. Pinning the invariant here catches a future
        refactor that accidentally loosens the ``> 1`` guard.
        """
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/x.py"),
                _file(source="b.j2", destination="out/x.py"),
            ]
        )

        assert len(result[0].sources) >= 2

    def test_finding_message_mentions_the_count(self) -> None:
        """The message includes how many sources are involved.

        A user needs to know whether they are fixing a two-way or a
        ten-way collision before they start. The message
        ("``3 sources map to the same destination``") conveys that at a
        glance, without needing to count the source list. The test
        pins this because the count is part of the *information* the
        finding carries, not part of the rendering — and it is the
        information a user triaging a large blueprint needs first.
        """
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/x.py"),
                _file(source="b.j2", destination="out/x.py"),
                _file(source="c.j2", destination="out/x.py"),
            ]
        )

        assert "3" in result[0].message


# ---------------------------------------------------------------------------
# Kind is irrelevant to collision detection
# ---------------------------------------------------------------------------
class TestKindDoesNotAffectCollision:
    """The detector groups by destination only, regardless of ``kind``.

    A binary copied to ``out/x`` and a template rendered to ``out/x``
    collide exactly as hard as two templates would. The fact that the
    writer *produces* the file differently does not change the fact
    that two writes go to the same path. Missing this case is a subtle
    bug: a naive detector that grouped by ``(destination, kind)``
    would pass the template-vs-template tests and silently allow a
    binary-vs-template collision to overwrite one file with another.
    """

    def test_render_and_copy_to_same_destination_collide(self) -> None:
        result = find_collisions(
            [
                _file(source="a.j2", destination="out/x", kind=PlannedFileKind.RENDER),
                _file(source="b.png", destination="out/x", kind=PlannedFileKind.COPY),
            ]
        )

        assert len(result) == 1
        assert result[0].code == "destination.collision"
        assert set(result[0].sources) == {"a.j2", "b.png"}

    def test_two_copies_to_same_destination_collide(self) -> None:
        result = find_collisions(
            [
                _file(source="a.png", destination="out/x", kind=PlannedFileKind.COPY),
                _file(source="b.png", destination="out/x", kind=PlannedFileKind.COPY),
            ]
        )

        assert len(result) == 1


# ---------------------------------------------------------------------------
# Input iteration contract
# ---------------------------------------------------------------------------
class TestInputIteration:
    """The detector accepts any iterable, not just a list.

    ``find_collisions`` is typed ``Iterable[PlannedFile]`` in its
    signature, and the caller (``FileMapper.map``) passes the result
    of a generator chain. The function must not assume the input has
    a ``len()`` or is indexable — it must consume it once. Pinning
    this contract here catches a refactor that adds a
    ``len(files)`` call "for the message" and quietly breaks every
    streaming caller.
    """

    def test_accepts_a_generator(self) -> None:
        def _gen():
            yield _file(source="a.j2", destination="out/x.py")
            yield _file(source="b.j2", destination="out/x.py")

        result = find_collisions(_gen())

        assert len(result) == 1
        assert set(result[0].sources) == {"a.j2", "b.j2"}

    def test_accepts_a_tuple(self) -> None:
        result = find_collisions(
            (
                _file(source="a.j2", destination="out/x.py"),
                _file(source="b.j2", destination="out/x.py"),
            )
        )

        assert len(result) == 1

    def test_input_is_consumed_once(self) -> None:
        """The detector must not iterate its input more than once.

        A one-shot iterator (generator, file handle) will yield nothing
        on a second pass. If a future refactor iterated twice — once to
        count, once to group — the second pass would see an empty
        input and the collision would silently disappear. The test
        uses a generator (which is one-shot by construction) and
        asserts the collision is still found, which is only possible
        if the input was consumed exactly once.
        """
        consumed: list[PlannedFile] = []

        def _gen():
            for f in [
                _file(source="a.j2", destination="out/x.py"),
                _file(source="b.j2", destination="out/x.py"),
            ]:
                consumed.append(f)
                yield f

        result = find_collisions(_gen())

        # ``consumed`` now has exactly two entries iff the generator was
        # iterated exactly once. A second iteration would append
        # nothing (the generator is exhausted), so the length is the
        # proof.
        assert len(consumed) == 2
        assert len(result) == 1
