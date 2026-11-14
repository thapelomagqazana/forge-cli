"""Copy-kind tests for `StagingEngine`.

Scope
-----
The staging engine distinguishes two kinds of planned files:

    * **render** — a Jinja template that goes through
      ``TemplateRenderer`` before being written to staging,
    * **copy** — a source file whose bytes are written to staging
      *verbatim*, without touching the renderer at all.

The kind decision is made by the *planner* (``forge.planning.
FileMapper``), which sets ``PlannedFile.kind`` from a small, documented
rule set (see ``docs/reference/PLANNING.md``). The engine's job is to
*honour* that decision. This file tests that the engine honours it
correctly.

Why copy-kind is a category of its own
--------------------------------------
Running a binary through Jinja is not "just slow" — it is a class of
bug that silently corrupts output in ways that are hard to attribute:

    * A PNG containing the bytes ``{{`` (which is legal image data)
      would either raise ``UndefinedError`` (if the byte sequence
      happens to look like template syntax) or, worse, be silently
      interpolated if the parser tolerated it.
    * A JSON fixture whose documented *content* happens to include
      ``{{ … }}`` — a blueprint for scaffolding a template-engine
      tutorial, say — would be mangled into whatever the current
      context resolved those braces to.
    * A vendored file (an SDK binary, a license PDF, a compiled
      asset) that the blueprint includes as part of a scaffold would
      be corrupted in place, with the corruption invisible until the
      generated project is opened.

The engine's contract is: **for ``COPY`` files, the byte sequence
between source and staged file is identical, with no intermediate
interpretation.** This file is where that contract is enforced.

What this file deliberately does *not* cover
--------------------------------------------
    * The *planner's* kind-detection rules — whether a given source
      extension maps to ``render`` or ``copy`` — are the planner's
      concern (see ``tests/unit/planning/test_literal_copy.py``).
      This file's fixtures ensure both kinds are present in the plan,
      but the assertions here are about the *engine's* behaviour given
      a plan, not about the planner's choices.

    * The *rendered* content of render-kind files — that is the
      happy-path file's concern (``test_engine_happy.py``). The tests
      here touch render-kind files only to *contrast* them with
      copy-kind files, and only to confirm the engine did not
      accidentally apply the copy path to a template.

    * The *failure* semantics of copy — what happens when a copy's
      source is missing or unreadable — are covered in
      ``test_engine_render_failure.py``, which exercises the abort-
      and-cleanup path uniformly for both kinds. The distinction
      there is a failure-mode distinction, not a kind distinction;
      keeping it in one place reflects that.

Why these tests use a real PNG fixture
--------------------------------------
A PNG is the canonical example of a file that looks like *nothing*
special to a text editor but contains arbitrary bytes. Using a
synthetic binary blob (bytes picked at random) would test the *same*
code path, but the PNG is more convincing as documentation: a reader
who sees "the engine copies a PNG verbatim" instantly understands
*why* copy-kind exists, in a way that "the engine copies bytes
``\\x00\\x01\\x02`` verbatim" does not.

The fixture binary (``tests/fixtures/staging/templates/assets/logo.png``)
is deliberately tiny and starts with the PNG magic bytes; it is not a
valid PNG beyond the header. That is documented in the fixture
README — the tests here never parse the image, only compare bytes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge.planning import FileMapper, PlannedFile, PlannedFileKind
from forge.staging import StagingEngine, StagingResult
from forge.verification import validate_blueprint_document

# Absolute path to the fixture tree. Same parents[] arithmetic as the
# other staging test files — see ``test_engine_happy.py``'s module
# docstring for the derivation.
FIX = Path(__file__).parents[2] / "fixtures" / "staging"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plan() -> list[PlannedFile]:
    """Load the fixture blueprint and produce its plan.

    Returns the plan only — the blueprint and variables are not needed
    by any test in this file (kind dispatch is about *file kinds*, not
    about the blueprint's shape). The plan result is guarded with an
    assertion so a broken fixture fails at the helper with a clear
    message, not three lines later in the test body.

    The helper *does* assert that both kinds are present in the plan.
    That is a fixture contract, not a test expectation: every test in
    this file contrasts render-kind against copy-kind, so a fixture
    that lost one kind would make the contrasts meaningless (or fail
    with a confusing "index out of range" from the test body). The
    guard lives in the helper so the failure message names the
    fixture problem at the helper, not at the assertion site.
    """
    document = yaml.safe_load((FIX / "blueprint.yaml").read_text())
    blueprint = validate_blueprint_document(document, source="blueprint.yaml")

    variables: dict[str, object] = {"project_name": "demo"}
    plan_result = FileMapper().map(blueprint, variables)
    assert plan_result.is_ok(), (
        "fixture is broken: FileMapper rejected the fixture blueprint. "
        f"Findings: {getattr(plan_result, 'error', None)!r}"
    )

    kinds = {p.kind for p in plan_result.value}
    assert kinds == {PlannedFileKind.RENDER, PlannedFileKind.COPY}, (
        "fixture is broken: the plan must contain at least one file of "
        "each kind for this file's tests to contrast them. Got kinds "
        f"{kinds!r}"
    )
    return plan_result.value


def _stage(
    tmp_path: Path,
    *,
    renderer: object | None = None,
) -> StagingResult:
    """Stage the fixture plan into a temporary destination root.

    Mirrors the helper in ``test_engine_happy.py`` — same arguments,
    same success guard — with one addition: the ``renderer`` keyword
    is forwarded to ``StagingEngine().stage(...)`` so tests that want
    to observe rendering can inject their own. The default (``None``)
    matches the engine's own default, so tests that do not care about
    the renderer get the same behaviour as before.

    Why the ``renderer`` parameter is typed ``object | None`` rather
    than ``TemplateRenderer | None``: this file's tests only care that
    whatever they pass ends up being invoked with the same signature
    the engine calls. Pinning the type to ``TemplateRenderer`` would
    force every spy to subclass it (which ``_RecordingRenderer`` does,
    for reasons the test explains), but a future test with a
    different injection strategy — say, a duck-typed recorder that
    happens to satisfy the interface — would be blocked by the
    annotation for no correctness reason. ``object | None`` is the
    permissive annotation; the actual constraint (the injected object
    must have a ``render(template_path, variables) -> Result`` method)
    is enforced by the engine at call time, which is where it
    matters.

    The caller is responsible for ``.destroy()`` — see the module
    docstring in ``test_engine_happy.py`` for why a pytest fixture is
    the wrong pattern here.
    """
    planned = _plan()
    result = StagingEngine().stage(
        planned,
        variables={"project_name": "demo"},
        blueprint_root=FIX,
        destination_root=tmp_path,
        renderer=renderer,  # type: ignore[arg-type]
    )
    assert result.is_ok(), (
        "fixture is broken: the happy-path plan must stage successfully. "
        f"Findings: {getattr(result, 'error', None)!r}"
    )
    return result.value


# ---------------------------------------------------------------------------
# Tests — the copy contract
# ---------------------------------------------------------------------------
def test_copy_kind_source_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    """The staged copy is byte-identical to its source.

    The direct expression of the copy contract. The test re-reads the
    fixture source *from disk* (not from the plan, not from the
    staging result's metadata) so the comparison is between two
    independent reads of the same file — one read of the original, one
    read of the staged copy. If the engine had silently re-encoded,
    re-formatted, or truncated the file, the two reads would differ.

    The choice of file matters: the fixture's ``logo.png`` is the
    canonical "bytes that are not text" case, and a text-file copy
    would pass even if the engine had accidentally routed it through
    a lossless text round-trip. A binary makes the copy-vs-render
    distinction unmistakable.

    The assertion uses ``==`` on ``read_bytes()`` rather than a hash
    comparison, because the fixture is small enough to compare
    directly and the failure message (a byte diff) is more diagnostic
    than a hash mismatch. A hash comparison is the right call for
    files that might be large; this one is not.
    """
    result = _stage(tmp_path)
    try:
        staged_logo = result.root / "src" / "demo" / "assets" / "logo.png"
        original_logo = (FIX / "templates" / "assets" / "logo.png").read_bytes()
        assert staged_logo.exists(), "copy-kind file is missing from staging"
        assert staged_logo.read_bytes() == original_logo, (
            "copy-kind file was not preserved byte-for-byte — the engine "
            "may be rendering binary files, or re-encoding them"
        )
    finally:
        result.destroy()


def test_copy_kind_file_contains_no_rendered_content(tmp_path: Path) -> None:
    """The staged copy does not contain the rendered value of any variable.

    A subtler failure mode than the previous test: an engine that
    rendered the file *and* then wrote the source bytes on top (or
    vice versa) would produce a file that is byte-identical to the
    source, passing the byte-equality test above, but the *renderer*
    would have been invoked on the binary — which is the actual bug
    (StrictUndefined firing on a PNG, say). The bug is that the
    renderer was called at all, not that its output survived.

    This test uses the fixture's variable value (``"demo"``, which is
    the resolved ``project_name``) to detect accidental rendering: if
    the binary had been through the renderer with the fixture's
    variables, either it would have raised (caught by the previous
    test's fixture sanity — the fixture does not raise for a real PNG
    header) or it would have produced the value ``"demo"`` somewhere
    in its bytes. The fixture PNG is 12 bytes and does not contain the
    ASCII sequence ``demo`` — so any ``demo`` in the staged copy would
    prove the file was touched by the renderer.

    The inverse case is checked too: the render-kind file *must*
    contain the interpolated value (covered by
    ``test_render_kind_is_actually_rendered`` below). Together, the
    two tests pin the kind dispatch in both directions, which is what
    makes "the engine honours the plan's kind" a checkable property.
    """
    result = _stage(tmp_path)
    try:
        staged_logo_bytes = (result.root / "src" / "demo" / "assets" / "logo.png").read_bytes()
        assert b"demo" not in staged_logo_bytes, (
            "copy-kind file contains the interpolated variable value "
            "(``demo``) — the engine rendered a file the planner marked "
            "as copy-kind"
        )
    finally:
        result.destroy()


def test_copy_kind_is_not_passed_through_renderer(tmp_path: Path) -> None:
    """The engine calls no renderer method on a copy-kind file.

    The strongest form of the copy contract: not just "the output
    looks right" but "the renderer was never invoked on this file."
    The previous tests verify the *result*; this one verifies the
    *means*. A regression that rendered the file and then discarded
    the result (or rendered to a temp file and copied the source over
    it) would pass the byte-equality test but fail this one.

    How the spy works
    -----------------
    The engine accepts an optional ``renderer`` argument
    (``StagingEngine.stage(..., renderer=...)``) which it forwards to
    ``render_or_copy`` for every planned file. This test passes a
    ``_RecordingRenderer`` — a ``TemplateRenderer`` subclass whose
    ``render`` records the template path and then delegates to the
    real implementation. After the run, the recorded paths are
    compared against the plan's render-kind sources:

        * every recorded source must be render-kind (no copy-kind
          source may appear),
        * every render-kind source must be recorded (the renderer
          must be consulted for every template).

    Both assertions together pin the dispatch in both directions: a
    regression that rendered a copy-kind file would fail the first;
    a regression that skipped rendering for a template would fail the
    second.

    Why the spy is injected, not monkeypatched onto the class
    ---------------------------------------------------------
    An earlier version of this test patched ``TemplateRenderer.render``
    on the class object:

        monkeypatch.setattr(TemplateRenderer, "render", _record_and_delegate)

    That version was fragile in a way that only surfaced when the
    class acquired a second name. ``forge.rendering.TemplateRenderer``
    (the façade alias the test imported) and
    ``forge.rendering.renderer.TemplateRenderer`` (the concrete class
    the engine's modules import) are not guaranteed to resolve to the
    same object at runtime — and when they did not, the patch landed
    on a class the engine never instantiated from, and the spy
    recorded nothing. The resulting failure looked like a *test*
    problem ("the spy fired zero times") but was actually a
    *mechanism* problem: the test was observing an implementation
    detail (which alias the engine imported) rather than the property
    under test.

    The fix is to use the seam the engine advertises. ``render_or_copy``
    already documents its ``renderer`` parameter as "for unit tests to
    pass a stub" — that is a design promise; the engine now honours it
    by forwarding the parameter through ``stage()``. The spy uses that
    seam, so it is independent of class identity, of which module the
    engine imports from, and of any future refactor that keeps the
    engine's public signature stable.

    Why the recorder is a *subclass* rather than a duck-typed object
    ------------------------------------------------------------------
    A duck-typed recorder (any object with a ``render`` method) would
    satisfy the engine at call time, but would obscure two things that
    make a future failure harder to diagnose:

        * The subclass's ``super().render(...)`` makes explicit that
          the real rendering still happens — the recorded file is
          actually rendered, not just reported as if it had been.
          A duck-typed recorder that fabricated a successful render
          would make the test pass on a *real* failure (the engine
          stopping calling render), because the recorder would keep
          "succeeding" regardless of the engine's behaviour.
        * The subclass keeps the test honest about what the engine's
          default renderer is. If a future refactor changed
          ``TemplateRenderer`` to a factory function, the subclass
          would fail to construct at test collection time — a loud,
          immediate signal that the injected type no longer matches
          the engine's expected interface. A duck-typed recorder
          would keep working, masking the interface change until a
          real caller broke.
    """
    from forge.rendering import TemplateRenderer

    recorded_sources: list[Path] = []

    class _RecordingRenderer(TemplateRenderer):
        """Records every ``render`` call, delegates the actual render."""

        def render(self, template_path, variables):  # type: ignore[override]
            recorded_sources.append(Path(template_path))
            return super().render(template_path, variables)

    result = _stage(tmp_path, renderer=_RecordingRenderer())
    try:
        # Every source the renderer was asked to render must be a
        # *render*-kind source. The plan's kind is the source of
        # truth; we derive the allowed sources from the plan rather
        # than hard-coding them, so a future fixture addition does
        # not require updating this test.
        planned = _plan()
        render_sources = {
            (FIX / p.source).resolve() for p in planned if p.kind is PlannedFileKind.RENDER
        }

        recorded_resolved = {p.resolve() for p in recorded_sources}
        illegal = recorded_resolved - render_sources
        assert not illegal, (
            "the renderer was invoked on copy-kind sources: "
            f"{sorted(illegal)!r}. Every entry in ``recorded_sources`` "
            "must correspond to a render-kind planned file. If the "
            "engine is calling ``TemplateRenderer.render`` for a "
            "copy-kind file — even if it discards the result — it is "
            "violating the copy contract."
        )

        # Sanity: the renderer must have been called for at least one
        # render-kind file. A regression that passed *nothing* to the
        # renderer (writing every source verbatim, losing interpolation
        # for every template) would otherwise pass the assertion above
        # for the wrong reason.
        assert render_sources <= recorded_resolved, (
            "the renderer was not invoked for some render-kind sources: "
            f"{sorted(render_sources - recorded_resolved)!r}. The engine "
            "must render every file the plan marks as render-kind."
        )
    finally:
        result.destroy()


# ---------------------------------------------------------------------------
# Tests — the contrast with render-kind
# ---------------------------------------------------------------------------
def test_render_kind_is_actually_rendered(tmp_path: Path) -> None:
    """Render-kind files are not copied verbatim — the variable is interpolated.

    The complement to the copy-kind assertions above. Together they
    pin the kind dispatch in both directions: a regression that made
    the engine treat *every* file as copy would fail this test (the
    template's ``{{ project_name }}`` would still be literal); a
    regression that made it treat every file as render would fail the
    byte-equality tests above (the binary would be corrupted or the
    renderer would raise on the PNG).

    Only the two directions together express "the engine honours
    ``PlannedFile.kind``." A single-direction test would pass for the
    wrong reason under one of the two regressions, and the two-sided
    version is what catches both.

    The assertion checks for *both* the presence of the interpolated
    value and the absence of the template syntax. Checking only one
    would miss the "renderer returned the input unchanged" case (which
    would have the syntax present but the value absent) and the
    "renderer emitted empty output" case (which would have neither).
    The two checks together pin the successful-render state.
    """
    result = _stage(tmp_path)
    try:
        rendered_main = (result.root / "src" / "demo" / "main.py").read_text(encoding="utf-8")

        assert "demo" in rendered_main, (
            "render-kind file does not contain the interpolated "
            "``project_name`` value — the engine may be copying the "
            "template verbatim instead of rendering it"
        )
        assert "{{" not in rendered_main, (
            "render-kind file still contains template syntax — the " "renderer was not invoked"
        )
    finally:
        result.destroy()


def test_copy_and_render_kinds_are_planned_independently(tmp_path: Path) -> None:
    """Each file is dispatched by its own kind, not by its sibling's.

    A subtle engine bug that would not be caught by the per-file tests:
    if the engine (incorrectly) derived kind from the *first* file in
    the plan and applied it to every subsequent file, the fixture's
    first file (``main.py.j2``, render-kind) would make every file
    render — including ``logo.png``. The previous tests would catch
    the "logo was rendered" symptom, but this test catches the
    *mechanism*: the engine is deciding kind per-file, not per-run.

    The assertion is that *both* kinds survived in their intended form
    in a single staging run. That is the property the fixture exists
    to exercise; this test is the positive statement of the fixture's
    purpose.
    """
    result = _stage(tmp_path)
    try:
        # Render-kind: the template was rendered.
        rendered = (result.root / "src" / "demo" / "main.py").read_text(encoding="utf-8")
        assert "demo" in rendered, (
            "render-kind file in a mixed-kind run was not rendered "
            "(template variable was not substituted)"
        )
        assert "{{" not in rendered, (
            "render-kind file in a mixed-kind run still contains " "unrendered template syntax"
        )

        # Copy-kind: the binary was copied verbatim.
        staged_logo = (result.root / "src" / "demo" / "assets" / "logo.png").read_bytes()
        original_logo = (FIX / "templates" / "assets" / "logo.png").read_bytes()
        assert (
            staged_logo == original_logo
        ), "copy-kind file in a mixed-kind run was not preserved verbatim"
    finally:
        result.destroy()
