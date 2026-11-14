"""Progress-event tests for `StagingEngine`.

Scope
-----
The staging engine reports progress through a ``ProgressSink`` — a
callable that receives a ``ProgressEvent`` for every observable step of
``stage()``. This file tests the *contract* between the engine and any
sink:

    * the engine emits exactly one event per planned file, plus a
      ``start`` at the beginning and a ``finish`` at the end,
    * the events arrive in the plan's order (not sorted, not batched),
    * every field the CLI's progress renderer relies on is populated
      (``index``, ``total``, ``path``, ``elapsed_ms``),
    * the ``stage`` label distinguishes render-kind from copy-kind
      files, because the CLI renders them differently,
    * a failing run still emits ``start`` and the events for files it
      processed before the failure — the sink is *advisory* and cannot
      prevent the engine from doing its job.

What this file deliberately does *not* cover
--------------------------------------------
    * The *rendering* of progress events (colour, carriage returns,
      terminal-vs-log formatting) — that is ``forge.cli.ui.
      print_progress``'s concern and is tested in
      ``tests/unit/cli/test_ui_progress.py``. This file tests what the
      engine emits, not what the CLI does with it.
    * The engine's happy-path file contents or cleanup semantics —
      those are covered in ``test_engine_happy.py``.
    * The engine's abort-on-failure semantics — covered in
      ``test_engine_render_failure.py``.

The split mirrors the codebase's phase boundary: the engine's job is
"emit typed events"; the sink's job is "render them"; the CLI wires the
two together. Testing each at its own layer means a regression names
the actual broken layer in its title.

Why a dedicated ``RecordingSink`` rather than a list + ``.append``
------------------------------------------------------------------
Two reasons:

1. **Identity of the sink matters.** A bare ``list`` is *also* a valid
   ``ProgressSink`` (it is callable and accepts one argument), but
   using one blurs the boundary: a caller reading the test cannot tell
   whether the engine *called* ``sink(event)`` or the test *appended*
   to a list after the fact. A named class says "this is the sink
   passed to the engine" in the type itself.
2. **Future extension is cheap.** If a test later needs to assert on
   the *number* of sink invocations, or record timestamps around
   each invocation, or raise from the sink to test that the engine
   tolerates a misbehaving sink, the recording class has a place to
   grow. A list would need to be replaced, and every caller of the
   test helper would need updating.

The class is deliberately minimal: it records, nothing else. Any
behaviour beyond recording (filtering, sequencing assertions) belongs
in the test body where it is visible, not hidden in the helper.

Why ``blueprint_root=FIX`` and ``destination_root=tmp_path``
------------------------------------------------------------
Both are required by ``StagingEngine.stage``'s signature, and neither
participates in progress emission. The test passes the actual fixture
root (because the engine needs it to resolve the copy-kind source) and
a real temporary directory (because the engine needs it to create the
staging sibling). The progress contract is independent of both — but
the engine must be given a valid configuration to run at all.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge.planning import FileMapper, PlannedFile
from forge.staging import ProgressEvent, StagingEngine
from forge.verification import validate_blueprint_document

# Absolute path to the fixture tree. Same parents[] arithmetic as the
# happy-path and failure-path test files — see
# ``test_engine_happy.py``'s module docstring for the derivation.
FIX = Path(__file__).parents[2] / "fixtures" / "staging"


# ---------------------------------------------------------------------------
# Recording sink
# ---------------------------------------------------------------------------
class RecordingSink:
    """A ``ProgressSink`` that records every event it receives.

    Implements the protocol structurally: any callable with the right
    signature is a valid sink, and this class is one. No inheritance
    from a base type is needed — the protocol is structural, and the
    engine's type annotation accepts it.

    The ``events`` list is exposed as a public attribute (not a method)
    because tests want to inspect it directly and repeatedly, and a
    method would add ceremony without benefit. The name is singular-
    purpose: it holds what the sink has recorded, nothing else.
    """

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plan() -> list[PlannedFile]:
    """Load the fixture blueprint and produce its plan.

    Returns the plan only — the blueprint and variables are not needed
    by any test in this file (progress is about *files*, not about the
    blueprint). The plan result is guarded with an assertion so a
    broken fixture fails at the helper with a clear message, not three
    lines later in the test body.
    """
    document = yaml.safe_load((FIX / "blueprint.yaml").read_text())
    blueprint = validate_blueprint_document(document, source="blueprint.yaml")

    variables: dict[str, object] = {"project_name": "demo"}
    plan_result = FileMapper().map(blueprint, variables)
    assert plan_result.is_ok(), (
        "fixture is broken: FileMapper rejected the fixture blueprint. "
        f"Findings: {getattr(plan_result, 'error', None)!r}"
    )
    return plan_result.value


def _stage_with_recording(tmp_path: Path) -> tuple[RecordingSink, object]:
    """Stage the fixture plan, recording every progress event.

    Returns ``(sink, result)``. The ``result`` is the ``Ok`` value
    (a ``StagingResult``), which the caller must ``destroy()`` — the
    sink itself needs no cleanup (it is a plain object).

    Two things this helper does *not* do, deliberately:

    * It does not assert on the result's ``Ok``-ness. A future test in
      this file might legitimately want to inspect progress from a
      failed run (the "advisory" property). Only the tests that need
      a successful result should assert it — the helper stays neutral.

    * It does not destroy the staging result. The caller owns the
      result's lifetime, and a test that raises an assertion before
      destroying it would leak the staging directory. The pattern
      used in this file is ``try/finally: result.destroy()`` around
      the test body — same discipline as ``test_engine_happy.py``.
      A helper that auto-destroyed would hide the pattern from a
      future contributor copying the test.
    """
    planned = _plan()
    sink = RecordingSink()
    result = StagingEngine().stage(
        planned,
        variables={"project_name": "demo"},
        blueprint_root=FIX,
        destination_root=tmp_path,
        progress=sink,
    )
    assert result.is_ok(), (
        "fixture is broken: the happy-path plan must stage successfully. "
        f"Findings: {getattr(result, 'error', None)!r}"
    )
    return sink, result.value


# ---------------------------------------------------------------------------
# Tests — event count and shape
# ---------------------------------------------------------------------------
def test_event_count_is_files_plus_start_and_finish(tmp_path: Path) -> None:
    """Exactly one event per file, plus lifecycle events.

    The engine's contract with the sink is: ``start`` + one event per
    planned file + ``finish``. A regression that emitted an extra
    event (say, a "validating" event) would break the CLI's progress
    *count* rendering — the user would see "5/4 files" — and this
    test is what catches that class.

    The assertion is on *counts*, not on the total length of the list,
    because a future engine might add new lifecycle events (e.g.
    ``"verify"``) and the count of *per-file* events is what must
    remain stable. Failing on the total length would make every
    future lifecycle addition a breaking test change, even though
    the per-file contract is unchanged.
    """
    planned = _plan()
    sink, result = _stage_with_recording(tmp_path)
    try:
        file_events = [e for e in sink.events if e.stage in {"render", "copy"}]
        lifecycle_events = [e for e in sink.events if e.stage in {"start", "finish"}]

        assert len(file_events) == len(planned), (
            f"expected exactly one event per planned file ({len(planned)}), "
            f"got {len(file_events)}"
        )
        assert len(lifecycle_events) == 2, (
            "expected exactly one ``start`` and one ``finish`` event; "
            f"got {[e.stage for e in lifecycle_events]!r}"
        )
    finally:
        result.destroy()


def test_start_is_first_and_finish_is_last(tmp_path: Path) -> None:
    """Lifecycle events frame every other event.

    The CLI's progress renderer starts a spinner on ``start`` and
    prints a summary on ``finish``. If ``finish`` were not last, the
    summary line would be printed before the last few files — a
    cosmetic bug that would confuse users on slow filesystems.

    The assertion is on the *ordering of stage labels*, not on
    individual instances, because the engine may emit multiple
    ``start``/``finish`` pairs in a future multi-plan refactor — the
    invariant is that the *first* event is a ``start`` and the *last*
    is a ``finish``, which is what a renderer that frames the run
    needs to know.
    """
    sink, result = _stage_with_recording(tmp_path)
    try:
        assert (
            sink.events[0].stage == "start"
        ), f"first event must be ``start``, got {sink.events[0].stage!r}"
        assert (
            sink.events[-1].stage == "finish"
        ), f"last event must be ``finish``, got {sink.events[-1].stage!r}"
    finally:
        result.destroy()


def test_indices_are_one_based_and_contiguous(tmp_path: Path) -> None:
    """Per-file events use 1-based, contiguous indices.

    The CLI renders the index as ``[N/M]``. A 0-based or non-contiguous
    sequence would produce ``[0/3]`` on the first file — confusing, and
    a contract break for any consumer computing percentage from
    ``index / total``.

    The assertion checks *contiguity*, not just 1-based-ness: a
    regression that skipped an index (say, because a file was
    processed without an event being emitted) would pass a "starts at
    1" check but fail the CLI's progress bar calculation. Contiguity
    is the property that makes the progress bar correct.
    """
    planned = _plan()
    sink, result = _stage_with_recording(tmp_path)
    try:
        file_events = [e for e in sink.events if e.stage in {"render", "copy"}]
        indices = [e.index for e in file_events]
        assert indices == list(range(1, len(planned) + 1)), (
            f"per-file indices must be 1..{len(planned)} in order, " f"got {indices!r}"
        )
    finally:
        result.destroy()


def test_total_is_constant_across_all_events(tmp_path: Path) -> None:
    """Every event reports the same ``total``.

    The CLI computes ``index / total`` for the progress bar. If
    ``total`` varied mid-run (say, because a file was skipped and
    ``total`` shrank), the percentage would jump backward — a jarring
    and misleading effect. The contract is that ``total`` is fixed at
    ``len(plan)`` for the whole run.

    The ``start`` event carries ``total`` too, because a renderer may
    want to display "0/M" before the first file event arrives (which
    is the correct thing to show while the first file is being
    processed). Asserting on the constant across *all* events is
    stronger than asserting on the first and last — it catches a
    future bug where ``total`` is recomputed per-file from the
    remaining plan.
    """
    planned = _plan()
    sink, result = _stage_with_recording(tmp_path)
    try:
        totals = {e.total for e in sink.events}
        assert totals == {len(planned)}, (
            f"every event must report total={len(planned)}; " f"got distinct totals {totals!r}"
        )
    finally:
        result.destroy()


# ---------------------------------------------------------------------------
# Tests — per-file fields
# ---------------------------------------------------------------------------
def test_file_events_carry_path_and_elapsed(tmp_path: Path) -> None:
    """Every per-file event names its destination and reports a duration.

    ``path`` is what the CLI renders next to the progress counter, and
    it must be the *destination-relative* path (not the absolute
    staging path, not the source path). A consumer that filtered by
    ``event.path.endswith(".py")`` would work only if the path is the
    destination — the one the user recognises.

    ``elapsed_ms`` is what a future `--verbose` mode would show and
    what a metrics collector would aggregate. It must be non-negative
    and finite — a regression that reported ``None`` or a negative
    value would silently poison any downstream aggregation.

    The ``>= 0`` check is the only assertion on the value itself; the
    *exact* value is inherently non-deterministic (it depends on the
    machine), so any test that pinned it would be flaky. The
    invariant is "an elapsed time is always reported", not "it takes
    0.5 ms".
    """
    sink, result = _stage_with_recording(tmp_path)
    try:
        file_events = [e for e in sink.events if e.stage in {"render", "copy"}]
        for event in file_events:
            assert event.path, f"per-file event has empty path: {event!r}"
            # The path is *destination-relative*: no leading ``/``, no
            # ``.forge-stage-`` prefix, no absolute filesystem path.
            # A regression that leaked the staging path would still
            # have a non-empty ``path`` — the assertion on the shape
            # is what catches it.
            assert not event.path.startswith(
                "/"
            ), f"per-file path must be relative, got absolute: {event.path!r}"
            assert not event.path.startswith(".forge-stage-"), (
                f"per-file path must be the destination, not the staging "
                f"location: {event.path!r}"
            )
            assert event.elapsed_ms >= 0, (
                f"per-file elapsed_ms must be non-negative, "
                f"got {event.elapsed_ms!r} for {event.path!r}"
            )
    finally:
        result.destroy()


def test_lifecycle_events_carry_no_path(tmp_path: Path) -> None:
    """``start`` and ``finish`` do not name a file.

    The ``path`` field is empty on lifecycle events by convention. The
    CLI relies on this: it renders the lifecycle summary differently
    from a per-file line, and a non-empty ``path`` on ``finish`` would
    suggest "the last file" — which is not what the summary is about.

    A regression that populated ``path`` on ``finish`` with the last
    file's path (a plausible "helpful" addition) would pass every
    other test in this file but mislead the CLI's renderer. This test
    pins the convention.
    """
    sink, result = _stage_with_recording(tmp_path)
    try:
        lifecycle = [e for e in sink.events if e.stage in {"start", "finish"}]
        for event in lifecycle:
            assert event.path == "", (
                f"lifecycle event {event.stage!r} must not carry a path; " f"got {event.path!r}"
            )
    finally:
        result.destroy()


# ---------------------------------------------------------------------------
# Tests — kind dispatch
# ---------------------------------------------------------------------------
def test_render_and_copy_events_use_distinct_stage_labels(tmp_path: Path) -> None:
    """Render-kind and copy-kind files emit different ``stage`` values.

    The CLI's progress renderer may want to show ``[render]`` for a
    template and ``[copy]`` for a binary asset — the labels tell the
    user what work the engine is doing, and the *two* labels are the
    vocabulary that makes that possible. The fixture deliberately
    includes both kinds (two ``.j2`` templates and one ``.png``
    asset), so a regression that emitted a single unified label
    would be caught here.

    The assertion is on *sets*, not on *counts*, because the fixture's
    exact composition may change (adding another template would change
    the count but not the presence of both labels). The property being
    tested — "both labels appear when both kinds are present" — is
    stable across fixture evolution.
    """
    sink, result = _stage_with_recording(tmp_path)
    try:
        labels = {e.stage for e in sink.events if e.stage in {"render", "copy"}}
        assert labels == {"render", "copy"}, (
            f"expected both ``render`` and ``copy`` labels in the " f"fixture run, got {labels!r}"
        )
    finally:
        result.destroy()


def test_copy_events_correspond_to_copy_kind_files(tmp_path: Path) -> None:
    """The ``copy`` label appears only for files the planner marked COPY.

    A regression that labelled *every* file ``render`` (or vice versa)
    would pass the previous test (both labels present) but mislead the
    progress renderer about what the engine was doing for each file.
    This test cross-checks the emitted labels against the plan's own
    kinds — the source of truth for "what should this file be."

    The mapping is derived from the plan, not hard-coded, so a future
    fixture edit (adding another copy-kind file) does not require
    updating this test. The invariant is "labels match kinds", not
    "these specific files are copies".
    """
    planned = _plan()
    expected_by_path = {p.destination: p.kind.value for p in planned}

    sink, result = _stage_with_recording(tmp_path)
    try:
        file_events = [e for e in sink.events if e.stage in {"render", "copy"}]
        for event in file_events:
            expected = expected_by_path[event.path]
            assert event.stage == expected, (
                f"event for {event.path!r} is labelled {event.stage!r}, "
                f"but the plan marks it {expected!r}"
            )
    finally:
        result.destroy()


# ---------------------------------------------------------------------------
# Tests — sink advisory property
# ---------------------------------------------------------------------------
def test_sink_is_not_consulted_for_correctness(tmp_path: Path) -> None:
    """The sink cannot prevent the engine from finishing its work.

    A ``ProgressSink`` is *advisory*: the engine calls it for
    observation, not decision. If a future refactor gave the sink
    veto power (returning ``False`` to skip a file, say), a buggy
    sink in production would silently corrupt the output — a class of
    coupling this test exists to prevent.

    The assertion is behavioural: the run completes successfully even
    when the sink does nothing useful (the default ``null_sink``, which
    is what this test uses). If the engine's behaviour changed based
    on sink *return values*, this test would fail as soon as the
    contract became non-advisory.

    The test does not use the ``RecordingSink`` — that is the point.
    A sink that does nothing must produce identical staging output to
    a sink that records everything. The test uses the null sink to
    prove the engine's outcome is independent of the sink's behaviour.
    """
    from forge.staging import null_sink

    planned = _plan()
    result = StagingEngine().stage(
        planned,
        variables={"project_name": "demo"},
        blueprint_root=FIX,
        destination_root=tmp_path,
        progress=null_sink,
    )
    try:
        assert result.is_ok(), (
            "a null progress sink must not affect staging success; "
            f"got {getattr(result, 'error', None)!r}"
        )
        # The staged tree is populated exactly as it would be with a
        # recording sink — the sink is observation, not participation.
        assert (result.value.root / "src" / "demo" / "main.py").exists()
    finally:
        result.value.destroy()


def test_engine_tolerates_a_sink_that_raises(tmp_path: Path) -> None:
    """A broken sink aborts the run — it does not corrupt the output.

    The engine treats the sink as advisory for *correctness* (the
    previous test), but it cannot *swallow* a sink exception: an
    exception from user-supplied code is a real failure, and the
    engine must propagate it rather than silently complete a run whose
    caller is misbehaving.

    The test asserts that a raising sink produces an exception (the
    sink's own) rather than a ``Result``. This documents the boundary
    between "advisory" (return values are ignored) and "untrusted
    code" (exceptions propagate). A future refactor that wrapped sink
    calls in ``try/except: pass`` would make a broken sink silently
    swallow files' progress events — exactly the class of "helpful"
    error handling that hides bugs.

    The engine's ``StagingArea`` context manager still runs its
    cleanup on the exception path — the staging directory is destroyed
    — so the test asserts no staging debris is left behind. That is
    the observable guarantee: even a misbehaving sink does not leak
    files.
    """
    import pytest

    planned = _plan()

    class _RaisingSink:
        def __call__(self, event: ProgressEvent) -> None:
            raise RuntimeError("sink is broken")

    with pytest.raises(RuntimeError, match="sink is broken"):
        StagingEngine().stage(
            planned,
            variables={"project_name": "demo"},
            blueprint_root=FIX,
            destination_root=tmp_path,
            progress=_RaisingSink(),
        )

    # The staging context manager's ``finally`` runs on the exception
    # path — no debris, no partial output.
    leftover = list(tmp_path.glob(".forge-stage-*"))
    assert leftover == [], (
        "a raising sink must not leave staging debris; " f"found {[p.name for p in leftover]!r}"
    )
