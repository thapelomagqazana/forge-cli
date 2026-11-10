"""Contract tests for the ``PlannedFile`` DTO and its public surface.

``PlannedFile`` is the *only* thing downstream code (the writer, future
``--dry-run --json`` renderers, any external tooling that consumes a
plan) reads from the planning phase. Everything else in
``forge.planning`` is an implementation detail. That makes
``PlannedFile`` a **public contract**, and this file is the executable
specification of that contract:

* **Field names are stable.** A consumer written against
  ``planned["destination"]`` must keep working across minor releases.
  Renaming a field is a breaking change and must be treated as such.

* **Field types are stable.** ``destination`` is a ``str``, ``kind``
  is a ``PlannedFileKind``, and the JSON serialisation uses the enum's
  *value* (``"render"`` / ``"copy"``), not its name. A consumer that
  branches on the string ``"render"`` is not broken when Python's enum
  repr changes.

* **Instances are immutable.** A planned file is a snapshot of an
  intended write. The writer must not mutate it, and a bug that tries
  to should surface at that line, not twenty frames later. Immutability
  also makes a plan safe to share across threads and to cache.

* **The DTO rejects anything it does not know.** ``extra="forbid"``
  means a typo in a caller's kwargs raises, rather than silently
  producing a field that is absent everywhere the consumer looks for
  it. This is a *stricter* contract than a plain dataclass and it is
  the correct one for a public DTO.

What a "contract test" is, and is not
-------------------------------------
A contract test asserts on the *observable API surface* — field names,
types, JSON shape, immutability, rejection of unknown fields — not on
internal behaviour. It is the test class that *must* be updated (with
a deliberate review conversation) when the contract changes, and it is
the trip wire that a future contributor hits when they try to rename
or reshape a public field "for cleanliness".

The tests here are deliberately *not* about planner behaviour (that is
``test_destinations.py``, ``test_collisions.py``, ``test_stable_order.py``,
``test_literal_copy.py``). They exercise ``PlannedFile`` in isolation,
because the contract is about the *type*, not about anything the
planner does with it.

Why the JSON assertions live here, not in a separate file
---------------------------------------------------------
``PlannedFile`` is expected to serialise as part of future
``--dry-run --json`` output and as part of any external tooling that
consumes a plan. That means the JSON shape is *part of the contract*,
not an implementation detail. Testing it beside the field-name
assertions keeps the contract's two faces (Python and JSON) in one
place — a reviewer updating one cannot miss the other.

Test layout
-----------
* ``TestFieldShape`` — the Python-side contract: names, types,
  defaults, ``extra="forbid"``, ``frozen=True``.
* ``TestKindEnum`` — the enum's values are the contract; they must
  be strings, not ints, and the enum must round-trip through its
  value.
* ``TestSerialisation`` — the JSON shape, exercised via
  ``model_dump(mode="json")``.
* ``TestConvenienceProperties`` — ``is_render`` / ``is_copy`` are
  the properties every caller branches on. They are part of the
  ergonomic contract, and they must be total (every valid kind maps
  to exactly one of them being ``True``).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from forge.planning import PlannedFile, PlannedFileKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _render(
    *,
    source: str = "templates/main.py.j2",
    destination: str = "src/main.py",
) -> PlannedFile:
    """Build a ``RENDER`` ``PlannedFile`` with defaults for fields not under test."""
    return PlannedFile(source=source, destination=destination, kind=PlannedFileKind.RENDER)


def _copy(
    *,
    source: str = "assets/logo.png",
    destination: str = "static/logo.png",
) -> PlannedFile:
    """Build a ``COPY`` ``PlannedFile`` with defaults for fields not under test."""
    return PlannedFile(source=source, destination=destination, kind=PlannedFileKind.COPY)


# ---------------------------------------------------------------------------
# The Python-side field contract
# ---------------------------------------------------------------------------
class TestFieldShape:
    """The Python-side fields, and the configuration they carry.

    These are the *names* a Python consumer writes in ``planned.source``
    and the *types* they receive back. A change to either is a breaking
    change, and these tests are the trip wire.
    """

    def test_exact_field_set(self) -> None:
        """The model has exactly three fields — no more, no less.

        Pinning the *exact* set (not just "at least these three")
        catches both a removed field (a breaking change for consumers)
        and an added field (also breaking — a consumer that round-trips
        a ``PlannedFile`` through a strict deserialiser would fail on
        the new field, and any new field that defaults to a value would
        silently change ``model_dump()`` output).

        An added field with a default is often *presented* as
        backward-compatible, but for a DTO whose whole purpose is to
        describe an intended write, "backwards-compatible" is not the
        property consumers care about — they care that the file they
        receive is described by the same fields they know. The test
        enforces that.
        """
        assert set(PlannedFile.model_fields) == {"source", "destination", "kind"}

    def test_source_is_a_string(self) -> None:
        """``source`` is a ``str`` — a path relative to the blueprint."""
        assert _render().source == "templates/main.py.j2"
        # The field type is what a consumer's type checker sees.
        annotation = PlannedFile.model_fields["source"].annotation
        assert annotation is str

    def test_destination_is_a_string(self) -> None:
        """``destination`` is a ``str`` — a resolved relative path."""
        assert _render().destination == "src/main.py"
        annotation = PlannedFile.model_fields["destination"].annotation
        assert annotation is str

    def test_kind_is_the_enum(self) -> None:
        """``kind`` is ``PlannedFileKind``, not a bare string."""
        assert _render().kind is PlannedFileKind.RENDER
        assert _copy().kind is PlannedFileKind.COPY

    def test_extra_fields_are_forbidden(self) -> None:
        """``extra="forbid"`` — an unknown kwarg is a ``ValidationError``.

        The alternative (silently dropping the unknown field) hides a
        caller's typo. In a plan that describes filesystem writes, a
        silent field drop is *worse* than an error: the plan proceeds
        missing information the caller tried to provide, and the
        missing information surfaces as a bug at write time.
        """
        with pytest.raises(ValidationError):
            # `destinaton` (missing an "i") is the classic typo.
            PlannedFile(
                source="a.j2",
                destinaton="out/a.py",  # type: ignore[call-arg]
                kind=PlannedFileKind.RENDER,
            )

    def test_instances_are_immutable(self) -> None:
        """``frozen=True`` — a validated plan cannot be mutated.

        The writer must not mutate a plan. A bug that tries to should
        surface at the line that tries, not three functions later when
        a *different* consumer sees the mutated value. ``frozen=True``
        makes that the case at runtime, not merely by convention.
        """
        planned = _render()
        with pytest.raises(ValidationError):
            planned.source = "elsewhere.j2"  # type: ignore[misc]

    def test_source_is_required(self) -> None:
        """Omitting ``source`` is a ``ValidationError``."""
        with pytest.raises(ValidationError):
            PlannedFile(  # type: ignore[call-arg]
                destination="out/a.py", kind=PlannedFileKind.RENDER
            )

    def test_destination_is_required(self) -> None:
        """Omitting ``destination`` is a ``ValidationError``."""
        with pytest.raises(ValidationError):
            PlannedFile(  # type: ignore[call-arg]
                source="a.j2", kind=PlannedFileKind.RENDER
            )

    def test_kind_is_required(self) -> None:
        """Omitting ``kind`` is a ``ValidationError``.

        ``kind`` has no default — the two kinds are semantically
        different (render vs. copy), and a plan that silently defaults
        to either would produce the wrong file. The default is the
        caller's job to choose, deliberately.
        """
        with pytest.raises(ValidationError):
            PlannedFile(  # type: ignore[call-arg]
                source="a.j2", destination="out/a.py"
            )

    def test_source_rejects_empty_string(self) -> None:
        """``min_length=1`` — an empty source is a structural error.

        A ``PlannedFile`` with an empty source is not a plan, it is a
        bug that reached the DTO. Rejecting it here means the writer
        never has to wonder whether an empty source means "intentionally
        empty" (impossible — sources name files) or "the planner
        messed up" (the real case).
        """
        with pytest.raises(ValidationError):
            PlannedFile(source="", destination="out/a.py", kind=PlannedFileKind.RENDER)

    def test_destination_rejects_empty_string(self) -> None:
        """``min_length=1`` — an empty destination is a structural error."""
        with pytest.raises(ValidationError):
            PlannedFile(source="a.j2", destination="", kind=PlannedFileKind.RENDER)


# ---------------------------------------------------------------------------
# The kind enum's public values
# ---------------------------------------------------------------------------
class TestKindEnum:
    """``PlannedFileKind``'s values are part of the contract.

    Every consumer that renders a plan (``--dry-run`` human output,
    ``--dry-run --json``, external tooling) switches on the *value* of
    the enum. The values are ``"render"`` and ``"copy"`` — strings, not
    integers, not enum names — because they appear directly in JSON
    output and in log lines.

    A future contributor tempted to "clean up" the enum by switching to
    integers or by renaming the values has to reckon with these tests:
    the enum's public surface is not an implementation detail.
    """

    def test_enum_members_are_exactly_render_and_copy(self) -> None:
        """No other kinds exist — no ``SYMLINK``, no ``TEMPLATE``, no ``LITERAL``.

        Pinning the exact member set catches an accidental ``LITERAL =
        "copy"`` alias (which would break ``is_rendered`` branching
        based on identity, not value) and a future "let's add a third
        kind" change that skipped the review conversation.
        """
        assert {member.name for member in PlannedFileKind} == {"RENDER", "COPY"}

    def test_values_are_strings(self) -> None:
        """Enum values are the wire form (``"render"``, ``"copy"``).

        The values appear in JSON, in logs, and in the human renderer's
        output. They must be strings so no encoder or cast is needed at
        any of those boundaries.
        """
        assert PlannedFileKind.RENDER.value == "render"
        assert PlannedFileKind.COPY.value == "copy"
        assert isinstance(PlannedFileKind.RENDER.value, str)

    def test_enum_subclasses_str(self) -> None:
        """``PlannedFileKind`` is a ``str`` subclass.

        This is what makes ``kind == "render"`` work in JSON-mode
        comparisons and what makes ``json.dumps({"kind": kind})``
        produce ``{"kind": "render"}`` without a custom encoder.
        Changing the base class to ``Enum`` (not ``str, Enum``) would
        break every consumer that relies on the interchangeable form.
        """
        assert issubclass(PlannedFileKind, str)

    def test_construction_from_value(self) -> None:
        """``PlannedFileKind("render")`` round-trips — used by JSON deserialisation."""
        assert PlannedFileKind("render") is PlannedFileKind.RENDER
        assert PlannedFileKind("copy") is PlannedFileKind.COPY

    def test_unknown_value_rejected(self) -> None:
        """An unknown wire value is a ``ValueError`` at construction.

        The ``match`` pins *which* value was rejected: a broad
        ``pytest.raises(ValueError)`` would also pass if the enum
        raised for a different reason (a bug in ``__init__``, a
        mismatch in a case-folded comparison, etc.), so the assertion
        names the offending input. The exact message is intentionally
        not pinned (it is Python's standard enum error and its wording
        varies across CPython minor versions); the substring
        ``"symlink"`` is stable across those versions because it is
        the value the caller passed.
        """
        with pytest.raises(ValueError, match="symlink"):
            PlannedFileKind("symlink")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSON serialisation contract
# ---------------------------------------------------------------------------
class TestSerialisation:
    """The JSON shape a consumer reads from ``--dry-run --json``.

    ``PlannedFile`` is expected to serialise cleanly via Pydantic's
    ``model_dump(mode="json")``. That mode *is* the contract: it is
    what any future serialiser for the plan will call, and it is what
    external tooling will receive. Testing the shape here, with the
    same API the future serialiser will use, ensures the two do not
    drift.

    Note that these tests do not go through ``.model_dump()`` (which
    yields enum objects in some modes) — they go through
    ``mode="json"``, because that is the mode whose shape is the
    public wire format.
    """

    def test_json_dump_has_expected_keys(self) -> None:
        """``model_dump(mode="json")`` has exactly three keys."""
        payload = _render().model_dump(mode="json")
        assert set(payload) == {"source", "destination", "kind"}

    def test_json_dump_kind_is_a_string(self) -> None:
        """The ``kind`` in JSON is the enum *value*, not its repr."""
        payload = _render().model_dump(mode="json")
        assert payload["kind"] == "render"
        assert isinstance(payload["kind"], str)

    def test_json_dump_source_and_destination_are_strings(self) -> None:
        """Paths serialise as strings."""
        payload = _render(source="templates/a.j2", destination="src/a.py").model_dump(mode="json")
        assert payload["source"] == "templates/a.j2"
        assert payload["destination"] == "src/a.py"

    def test_json_dump_is_round_trip_safe(self) -> None:
        """``json.dumps`` of the dump is valid JSON, and re-parses."""
        payload = _render().model_dump(mode="json")
        text = json.dumps(payload)
        reparsed = json.loads(text)
        assert reparsed == payload

    def test_round_trip_back_to_model(self) -> None:
        """A dumped ``PlannedFile`` can be rebuilt via ``model_validate``.

        The reverse direction is what a future "read a plan back" tool
        will call. If the JSON shape is not round-trip-safe, either
        deserialising a persisted plan or reserialising one would fail
        — both are future use cases the shape exists to support.
        """
        original = _render(source="a.j2", destination="out/a.py")
        rebuilt = PlannedFile.model_validate(original.model_dump(mode="json"))
        assert rebuilt == original

    def test_copy_kind_serialises_to_copy(self) -> None:
        """A ``COPY`` plan serialises as ``"copy"``, not ``"COPY"``."""
        payload = _copy().model_dump(mode="json")
        assert payload["kind"] == "copy"


# ---------------------------------------------------------------------------
# Convenience properties
# ---------------------------------------------------------------------------
class TestConvenienceProperties:
    """``is_render`` and ``is_copy`` are the branches every consumer takes.

    Both properties exist so call sites read as English (``if
    planned.is_render:``) and so there is exactly one way to ask each
    question. They must be **total and disjoint** over the set of valid
    kinds: every ``PlannedFile`` has exactly one of them ``True``.

    A future contributor tempted to add a third ``PlannedFileKind``
    without updating these properties has to reckon with these tests —
    the disjoint-and-total check fires the moment the enum and the
    properties disagree.
    """

    def test_render_kind_sets_is_render_true(self) -> None:
        planned = _render()
        assert planned.is_render is True
        assert planned.is_copy is False

    def test_copy_kind_sets_is_copy_true(self) -> None:
        planned = _copy()
        assert planned.is_copy is True
        assert planned.is_render is False

    def test_render_and_copy_are_disjoint(self) -> None:
        """For every kind, exactly one of the two properties is ``True``."""
        for kind in PlannedFileKind:
            planned = PlannedFile(source="a", destination="b", kind=kind)
            assert planned.is_render != planned.is_copy, (
                f"{kind!r}: is_render and is_copy must be mutually exclusive "
                f"and one of them must be True"
            )

    def test_properties_are_total(self) -> None:
        """For every kind, at least one property is ``True``.

        A future contributor adding ``PlannedFileKind.SYMLINK`` without
        updating ``is_render`` and ``is_copy`` would fail this test:
        ``SYMLINK.is_render`` and ``SYMLINK.is_copy`` would both be
        ``False``, and the writer would silently skip the file.
        """
        for kind in PlannedFileKind:
            planned = PlannedFile(source="a", destination="b", kind=kind)
            assert planned.is_render or planned.is_copy, (
                f"{kind!r}: neither is_render nor is_copy is True — the "
                f"writer would silently skip this file"
            )


# ---------------------------------------------------------------------------
# Equality contract
# ---------------------------------------------------------------------------
class TestEquality:
    """Field-by-field equality makes plans comparable and testable.

    Two ``PlannedFile`` instances with the same fields compare equal.
    This is what lets a plan be asserted against a golden fixture in a
    single ``assert actual == expected`` (rather than comparing each
    field), and what makes ``set``/``dict`` keys work when a future
    consumer wants to key on a planned file.
    """

    def test_equal_fields_compare_equal(self) -> None:
        a = _render(source="a.j2", destination="out/a.py")
        b = _render(source="a.j2", destination="out/a.py")
        assert a == b

    def test_different_fields_compare_unequal(self) -> None:
        a = _render(source="a.j2", destination="out/a.py")
        b = _render(source="a.j2", destination="out/b.py")
        assert a != b

    def test_different_kinds_compare_unequal(self) -> None:
        a = PlannedFile(source="a.j2", destination="out/a.py", kind=PlannedFileKind.RENDER)
        b = PlannedFile(source="a.j2", destination="out/a.py", kind=PlannedFileKind.COPY)
        assert a != b

    def test_hashable(self) -> None:
        """``frozen=True`` makes instances hashable.

        A planned file being hashable is not in the *current* contract,
        but it is a consequence of ``frozen=True`` that a future
        consumer might rely on (say, to key a ``dict[PlannedFile, str]``
        of warnings). Pinning it means a future "make it a plain
        dataclass" change is caught.
        """
        a = _render()
        b = _render()
        assert hash(a) == hash(b)
        assert {a, b} == {a}
