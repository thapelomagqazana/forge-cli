"""Happy-path tests for `StagingEngine`.

Scope
-----
These tests cover the *success* path of ``StagingEngine.stage()``:

    * every planned file is produced in the staging tree,
    * the directory structure of the destination is preserved,
    * render-kind files are rendered, copy-kind files are copied verbatim,
    * the caller receives a ``StagingResult`` they can consume,
    * the destination root is not polluted by staging artefacts.

Failure-path tests (render errors, cleanup, destination byte-identity)
live in ``test_engine_render_failure.py``. Progress-event tests live in
``test_engine_progress.py``. Invisibility assertions live in
``test_engine_invisibility.py``. Keeping the happy path in its own file
means a failing success test points a reader at "the engine did not
produce what it promised" — not at "the engine's error handling is
broken" or "the engine's progress reporting is wrong." One concern per
file is what lets a CI failure name the actual regression in its title.

Fixture sanity tests
--------------------
The first two tests in this file are **not** tests of the engine. They
assert the fixture's own documented invariants:

    * the ``.j2`` templates end with exactly one newline,
    * the ``.png`` binary starts with the PNG signature and contains NULs.

Those invariants are what the engine tests below *assume*. If a
fixture drifts (e.g. an editor strips a trailing newline on save — a
realistic failure when the global ``.editorconfig`` sets
``trim_trailing_whitespace = true``), the drift would otherwise surface
three layers up as a confusing rendering-output diff. The sanity tests
make the failure name the *fixture* — which is where the fix is —
rather than the *rendered string* — which is a symptom of the fixture
bug, not a bug of its own.

Keeping the sanity tests in this file (rather than a separate
``test_fixtures.py``) means a developer editing a fixture sees both
the sanity failure *and* the affected engine test in the same file, in
one test run, with matching line numbers. That proximity is what makes
the fix obvious.

Why a single ``_plan()`` helper
-------------------------------
Every test in this file needs the same three things: the fixture
blueprint, the variable mapping that resolves it, and a ``PlannedFile[]``
list built from both. Duplicating that setup eight times would bury the
assertions under boilerplate and drift the moment the fixture's
variable name changed. The helper is *file-local* on purpose — it is
not shared with the other staging tests because those tests do not want
the same variables (the failure fixtures need *fewer*), and centralising
it in a ``conftest.py`` would obscure which tests depend on which
fixture shape.

Why ``FIX`` uses ``parents[2]``
-------------------------------
``__file__`` is ``tests/unit/staging/test_engine_happy.py``:

    parents[0] → tests/unit/staging/
    parents[1] → tests/unit/
    parents[2] → tests/
    parents[3] → repo root

So ``parents[2] / "fixtures" / "staging"`` resolves to the fixture
directory. This is stable under pytest's rootdir detection (which
does not alter ``__file__``) and under any working directory the
test is invoked from (which ``__file__`` is independent of, by
construction). Using ``os.getcwd()`` or ``Path("tests/fixtures/...")``
would break in both cases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from forge.planning import FileMapper, PlannedFile
from forge.staging import StagingEngine, StagingResult
from forge.verification import validate_blueprint_document

# Absolute path to the fixture tree. See the module docstring for the
# parents[] arithmetic.
FIX = Path(__file__).parents[2] / "fixtures" / "staging"


# ---------------------------------------------------------------------------
# Fixture sanity — the input's own invariants
# ---------------------------------------------------------------------------
# These assertions are not testing the engine. They are testing that
# the *input* to the engine still matches the contract that the rest
# of this file assumes. A drift in the fixture (a template that lost
# its trailing newline during an editor save, a binary that was
# re-encoded by a text editor) would otherwise fail three layers up
# with a rendering-output diff — a message that names the *symptom*
# and hides the *cause*. Running the invariant checks first means the
# failure names the file to fix.
#
# See tests/fixtures/staging/README.md's "Invariants tests rely on"
# section for the documentation these tests enforce.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "template",
    ["templates/main.py.j2", "templates/pyproject.toml.j2"],
)
def test_fixture_template_ends_with_exactly_one_newline(template: str) -> None:
    """Fixtures end with exactly one ``\\n``; see the fixture README.

    The renderer preserves trailing newlines (``keep_trailing_newline=
    True`` in ``forge.rendering.environment``), so a template that is
    missing its final newline changes the *rendered* output. Without
    this test, that failure surfaces in
    ``test_all_files_staged_in_preserved_structure`` as a diff of the
    rendered string — a real failure, but a confusing one, because the
    fix is in the fixture, not the engine.

    Rejecting two newlines as well as zero is deliberate: the fixture
    is documented as ending with exactly one, and a template ending in
    ``\\n\\n`` would also produce the wrong rendered output. The test
    enforces the documented invariant exactly, not approximately.

    Failure-mode guidance in the assertion message names
    ``.editorconfig`` because that is the *most likely* cause of a
    regression: the repository's global config sets
    ``trim_trailing_whitespace = true`` and only overrides it for
    fixture paths. A contributor who removes the
    ``[tests/fixtures/**/*.j2]`` section will reintroduce this bug
    silently — the failure message points them at the fix.
    """
    data = (FIX / template).read_bytes()
    assert data.endswith(b"\n"), (
        f"fixture {template} does not end with a newline. The renderer "
        "preserves trailing newlines, so the rendered output would be "
        "missing one. Re-add the newline and re-run. If an editor keeps "
        "stripping it, check that .editorconfig has the "
        "[tests/fixtures/**/*.j2] section (trim_trailing_whitespace = "
        "false). See tests/fixtures/staging/README.md."
    )
    assert not data.endswith(b"\n\n"), (
        f"fixture {template} ends with more than one newline; the "
        "fixture is documented as ending with exactly one."
    )


def test_fixture_binary_is_not_text_encoded() -> None:
    """``logo.png`` starts with the PNG magic bytes and contains NULs.

    The staging engine's copy-kind branch relies on this fixture being
    unambiguously binary. If a future contributor "cleans up" the file
    by decoding and re-encoding it — a realistic mistake when editing
    fixtures in a text editor — the bytes would change and
    ``test_binary_copied_verbatim`` would still pass, because it
    compares source to staged output and both would be wrong in the
    same way. This test asserts the invariant against the *fixture
    itself*, not against a copy of it.

    The NUL-byte check catches the specific failure mode of a text
    editor re-saving a binary: NULs are the first thing a text editor
    drops (or replaces with the Unicode replacement character), and
    their presence proves the file has not been round-tripped through
    a text-only pipeline.
    """
    data = (FIX / "templates" / "assets" / "logo.png").read_bytes()
    assert data.startswith(b"\x89PNG"), (
        "fixture logo.png does not start with the PNG signature; the "
        "file may have been re-encoded by a text editor. Restore it "
        "with the bytes documented in tests/fixtures/staging/README.md."
    )
    assert b"\x00" in data, (
        "fixture logo.png contains no NUL bytes; a binary that has "
        "lost its NULs has almost certainly been round-tripped through "
        "a text editor. Restore it with the bytes documented in "
        "tests/fixtures/staging/README.md."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plan() -> tuple[object, dict[str, object], list[PlannedFile]]:
    """Load the fixture blueprint and produce its plan.

    Returns ``(blueprint, variables, planned_files)``. The first two
    elements are typed ``object`` / ``dict[str, object]`` rather than
    ``Blueprint`` / ``Mapping`` to avoid importing the model types into
    a test file that does not otherwise need them; assertions in this
    file are about the *staging output*, not the model shape, and a
    reader who needs the blueprint can inspect it via the fixture YAML.

    The ``assert plan_result.is_ok()`` is a *guard*, not a test — if
    the fixture's own planning fails, every test in this file would
    fail with a confusing ``AttributeError`` on ``.value`` three lines
    later. Asserting on the precondition makes the failure name the
    actual problem ("fixture is broken") at the actual place.
    """
    document = yaml.safe_load((FIX / "blueprint.yaml").read_text())
    blueprint = validate_blueprint_document(document, source="blueprint.yaml")

    variables: dict[str, object] = {"project_name": "demo"}
    plan_result = FileMapper().map(blueprint, variables)
    assert plan_result.is_ok(), (
        "fixture is broken: FileMapper rejected the fixture blueprint. "
        f"Findings: {getattr(plan_result, 'error', None)!r}"
    )
    return blueprint, variables, plan_result.value


def _stage(tmp_path: Path) -> StagingResult:
    """Stage the fixture plan into a temporary destination root.

    Every test needs ``StagingEngine().stage(...)`` with the same
    arguments and the same success guard. Wrapping it means (a) the
    assertion on ``result.is_ok()`` happens once, (b) the test body
    focuses on the *staging output*, and (c) a helper that needs to
    grow (e.g. to inject a ``progress`` sink for a future test) grows
    in one place.

    The caller is responsible for calling ``.destroy()`` on the
    returned ``StagingResult`` — see the fixture docstring and
    ``test_destination_untouched_on_success_before_commit`` for why
    a ``try/finally`` around each test body is the right pattern here
    rather than a pytest fixture (which would obscure that the result
    *owns* a directory until explicitly destroyed).
    """
    _blueprint, variables, planned = _plan()
    result = StagingEngine().stage(
        planned,
        variables=variables,
        blueprint_root=FIX,
        destination_root=tmp_path,
    )
    assert result.is_ok(), (
        "fixture is broken: the happy-path plan must stage successfully. "
        f"Findings: {getattr(result, 'error', None)!r}"
    )
    return result.value


# ---------------------------------------------------------------------------
# Tests — file contents and structure
# ---------------------------------------------------------------------------
def test_all_files_staged_in_preserved_structure(tmp_path: Path) -> None:
    """Every planned file is present in staging, in the correct structure.

    This is the acceptance-criteria test for "all planned files
    rendered into staging" + "directory structure preserved". It
    exercises three distinct cases in one assertion set:

        1. A *render*-kind file in a nested directory
           (``src/demo/main.py``) whose rendered content must reflect
           the interpolated variable.
        2. A *render*-kind file at the root (``pyproject.toml``) to
           prove flat and nested destinations are handled identically.
        3. A *copy*-kind file in a nested directory
           (``src/demo/assets/logo.png``) whose bytes must be preserved
           verbatim.

    If this test fails, the whole happy path is broken — hence its
    place near the top of the file (after the fixture-sanity tests).
    Subsequent tests in this file are narrower (they check destroy
    semantics, kind handling, absence of debris) and assume this one's
    invariants.
    """
    result = _stage(tmp_path)
    try:
        staged = result.root

        # (1) render-kind, nested.
        #
        # ``read_text()`` decodes UTF-8 by default (matching what the
        # staging engine wrote). The assertion is exact equality — not
        # ``in`` or ``startswith`` — because the fixture's trailing
        # newline is a documented invariant (see the fixture-sanity
        # tests above). An exact equality here is what catches the
        # "fixture lost its newline" bug that the sanity tests are
        # also guarding against; two layers, one contract, because the
        # contract is worth defending twice.
        main_py = staged / "src" / "demo" / "main.py"
        assert main_py.exists(), "render-kind file in a nested directory is missing from staging"
        assert main_py.read_text(encoding="utf-8") == 'print("Hello demo")\n', (
            "render-kind file content is wrong — either the template was "
            "not rendered, or `project_name` was not interpolated, or the "
            "template lost its trailing newline (run "
            "test_fixture_template_ends_with_exactly_one_newline to check "
            "the fixture itself)."
        )

        # (2) render-kind, root-level.
        pyproject = staged / "pyproject.toml"
        assert pyproject.exists(), "render-kind file at the destination root is missing"
        rendered_pyproject = pyproject.read_text(encoding="utf-8")
        assert (
            'name = "demo"' in rendered_pyproject
        ), "root-level render did not interpolate `project_name`"

        # (3) copy-kind, nested, byte-for-byte preservation.
        logo = staged / "src" / "demo" / "assets" / "logo.png"
        assert logo.exists(), "copy-kind file in a nested directory is missing"
        original = (FIX / "templates" / "assets" / "logo.png").read_bytes()
        assert logo.read_bytes() == original, (
            "copy-kind file was mutated during staging — likely rendered "
            "instead of copied, or re-encoded"
        )
        # Belt-and-braces: verify the magic bytes so a fixture change is
        # caught even if `original` and `logo` both change together.
        assert logo.read_bytes().startswith(b"\x89PNG"), (
            "logo.png does not start with the PNG signature — the fixture "
            "may have been corrupted"
        )
    finally:
        result.destroy()


def test_staging_result_manifest_matches_plan(tmp_path: Path) -> None:
    """``StagingResult.manifest`` lists exactly the planned destinations.

    The manifest is the caller's contract for what was staged. If it
    diverges from the plan, a downstream commit phase could move the
    wrong files — or miss files entirely. The assertion is set-equality
    on destinations (order is guaranteed separately by the sorted
    manifest invariant) plus a spot-check on the kind field, which is
    the only other information the manifest carries.
    """
    _blueprint, _variables, planned = _plan()
    expected_destinations = sorted(p.destination for p in planned)

    result = _stage(tmp_path)
    try:
        manifest_destinations = [dest for dest, _kind in result.manifest]
        assert manifest_destinations == expected_destinations, (
            "manifest destinations do not match the plan; a downstream "
            "commit would move the wrong files"
        )

        # Kind field: render vs copy. The map is small enough to
        # hard-code here; a future fixture with more entries should
        # extend this test rather than split it.
        kinds = dict(result.manifest)
        assert kinds["src/demo/main.py"] == "render"
        assert kinds["pyproject.toml"] == "render"
        assert kinds["src/demo/assets/logo.png"] == "copy"
    finally:
        result.destroy()


def test_staging_root_is_sibling_of_destination_root(tmp_path: Path) -> None:
    """The staging tree is a *sibling* of the destination root.

    Three properties matter, and the test asserts all three
    independently because each is a distinct consequence of the
    placement contract:

        1. **Same filesystem.** ``os.replace`` is atomic only within
           a filesystem. The staging root and the destination root
           must be on the same device, or the commit phase's atomic
           per-file rename degrades to a non-atomic copy. The
           assertion compares ``st_dev`` — the exact check
           ``os.replace`` makes internally.

        2. **Sibling, not child.** The staging root's parent is
           ``tmp_path.parent``, not ``tmp_path``. Staging in the
           destination's parent (rather than inside the destination)
           is what allows a failed run to leave no trace: the
           destination root is never created just to hold the
           staging tree, so a failure before commit leaves the
           filesystem exactly as it was. See ADR-0019 and
           ``forge.safety.staging``'s "Where the staging directory
           lives" section.

        3. **Not inside the destination.** ``result.root`` is not a
           descendant of ``tmp_path``. This is the specific
           regression the sibling-placement design exists to
           prevent — an earlier design staged inside the
           destination, which worked for atomicity but forced the
           destination to be created before staging could begin,
           leaking an empty directory on any failed run. The
           assertion catches that regression at the engine's level,
           complementing the safety-layer test that catches it at
           the ``StagingArea`` level.

    Why three assertions instead of one: each expresses a distinct
    consequence of the placement. An implementation that satisfied
    only "sibling" (say, by staging in a sibling of the parent —
    ``tmp_path.parent.parent``) would pass assertion 2 but fail
    assertion 1 if it crossed a filesystem boundary, and fail
    nothing if it happened to remain on the same device. Assertion
    3 is the "sibling" property stated directly; assertion 2 is the
    same property plus "one level up, not deeper." The three
    together pin the placement contract precisely, so a future
    refactor that changes the placement strategy fails at the
    specific property it broke, not at a diffuse "the staging root
    is in the wrong place" message.
    """
    result = _stage(tmp_path)
    try:
        staging_root = result.root
        destination_root = tmp_path.resolve()

        # (1) Same filesystem. Compare ``st_dev`` on the two paths —
        # the exact check ``os.replace`` makes internally, and the
        # property that makes the atomicity guarantee meaningful.
        staging_dev = os.stat(staging_root).st_dev
        destination_dev = os.stat(destination_root).st_dev
        assert staging_dev == destination_dev, (
            f"staging root {staging_root} (device {staging_dev}) is on a "
            f"different filesystem from destination root "
            f"{destination_root} (device {destination_dev}) — "
            "cross-filesystem staging breaks atomic commit "
            "(see ADR-0019)"
        )

        # (2) Sibling, not child. The staging root's parent must be
        # the destination's parent, not the destination itself.
        assert staging_root.parent == destination_root.parent, (
            f"staging root {staging_root} must be a direct sibling of "
            f"the destination root {destination_root}. Expected parent "
            f"{destination_root.parent}, got {staging_root.parent}. "
            "Staging in the destination's parent (not inside the "
            "destination) is what allows a failed run to leave no "
            "trace — see ADR-0019 and forge.safety.staging's 'Where "
            "the staging directory lives' section."
        )

        # (3) Not inside the destination. This is the specific
        # regression the sibling-placement design exists to prevent:
        # an earlier design staged inside the destination and forced
        # the destination to be created before staging could begin,
        # leaking an empty directory on any failed run.
        assert not staging_root.is_relative_to(destination_root), (
            f"staging root {staging_root} must not be inside the "
            f"destination root {destination_root} — the earlier "
            "design (staging inside the destination) is exactly what "
            "the sibling-placement fix reversed"
        )
    finally:
        result.destroy()


def test_destination_untouched_on_success_before_commit(tmp_path: Path) -> None:
    """Staging does not write to the destination; only commit will.

    The engine's entire safety story (ADR-0019) is that staging
    *cannot* pollute the destination — files appear there only when
    the commit phase moves them. This test proves that invariant at
    the point where it is easiest to break: the success path.

    Two assertions:

        1. The staging tree exists (the engine did produce something).
        2. Every entry *visible* under ``tmp_path`` (i.e. not the
           staging directory itself) pre-existed staging.

    The second assertion is what catches the realistic regression:
    someone "helpfully" writing a lock file, a marker, or a summary at
    the destination root from inside the engine. Those artefacts would
    be visible to the user in their project directory, which is exactly
    the surprise staging exists to prevent.

    Note the "visible under ``tmp_path``" filter: since staging is a
    *sibling* of ``tmp_path`` (not a child), a staging directory never
    appears in ``tmp_path.iterdir()`` at all. The filter here is
    defensive — it keeps the test correct under the *old* placement
    too, in case a future refactor reverts the sibling change. That
    redundancy is cheap and the test's claim ("the destination stays
    clean") is stronger for it.
    """
    # Seed the destination with a pre-existing file so we have a
    # non-empty baseline. An empty directory would make the visible-set
    # comparison trivially pass even if the engine wrote a file and
    # then deleted it (a hypothetical regression this test is guarding
    # against).
    seed = tmp_path / "existing.txt"
    seed.write_text("hello")
    visible_before = sorted(p.name for p in tmp_path.iterdir())

    result = _stage(tmp_path)
    try:
        # The staging tree is a hidden (dot-prefixed) sibling; the
        # user's "project directory" is everything else.
        visible_after = sorted(
            p.name for p in tmp_path.iterdir() if not p.name.startswith(".forge-stage-")
        )
        assert visible_after == visible_before, (
            "staging wrote visible files into the destination before commit — "
            "the caller is not supposed to see project files until the "
            "commit phase moves them (see ADR-0019)"
        )
    finally:
        result.destroy()


def test_destroy_is_idempotent(tmp_path: Path) -> None:
    """``StagingResult.destroy()`` can be called twice without error.

    The pattern every caller should use is ``try/finally:
    result.destroy()`` — but a caller that already destroyed the
    result explicitly (e.g. after a downstream phase consumed the
    tree) would hit a double-destroy in the ``finally``. Idempotence
    is the property that makes that pattern safe without forcing
    callers to guard the call.

    This is not a hypothetical: the eventual ``_commit_staged``
    function in ``cli/commands/create.py`` destroys the result on any
    exit path, and a future feature that also destroys on *success*
    would double-destroy without this guarantee.
    """
    result = _stage(tmp_path)
    result.destroy()
    # Should not raise, even on a second call. If this raises, the
    # ``try/finally: result.destroy()`` pattern in every caller is
    # quietly broken.
    result.destroy()


def test_destroyed_tree_is_gone_from_disk(tmp_path: Path) -> None:
    """``destroy()`` actually removes the tree, not just marks it.

    The idempotence guarantee (above) is only useful if the first call
    does real work. This test asserts the observable effect: after
    ``destroy()``, the staging root no longer exists on disk.

    A regression that made ``destroy()`` a no-op (perhaps by accident,
    perhaps because someone thought the context manager already
    handled it — it does not, see ``result.py``'s ``__exit__``
    docstring) would pass the idempotence test but leak disk space
    forever. This test catches that.
    """
    result = _stage(tmp_path)
    root = result.root
    assert root.exists(), "staging root should exist immediately after stage()"
    result.destroy()
    assert not root.exists(), (
        "destroy() did not remove the staging tree from disk — this is a " "disk leak"
    )


# ---------------------------------------------------------------------------
# Tests — kind dispatch
# ---------------------------------------------------------------------------
def test_binary_copied_verbatim(tmp_path: Path) -> None:
    """Copy-kind files bypass the renderer entirely.

    The planner decides kind (render vs copy) from the source
    extension; the engine's job is to honour that decision. A regression
    here would either corrupt binaries (rendering them through Jinja,
    where a stray ``{{`` in an image would raise ``UndefinedError`` or
    — worse — silently interpolate) or pass templates through as
    literal text (a ``print("Hello {{ project_name }}")`` in the
    output).

    The test re-reads the fixture source directly (not via the engine)
    so the assertion is against the *original bytes*, not against a
    copy the engine could have corrupted identically twice.
    """
    result = _stage(tmp_path)
    try:
        staged_logo = result.root / "src" / "demo" / "assets" / "logo.png"
        original_logo = (FIX / "templates" / "assets" / "logo.png").read_bytes()
        assert staged_logo.read_bytes() == original_logo, (
            "copy-kind file was not preserved byte-for-byte — the engine "
            "may be rendering binary files"
        )
    finally:
        result.destroy()


def test_render_kind_files_are_not_copied_verbatim(tmp_path: Path) -> None:
    """Render-kind files go through Jinja — the variable is interpolated.

    The complement to ``test_binary_copied_verbatim``. A regression
    that made the engine treat every file as copy-kind would pass the
    binary-preservation test (files would still match their sources)
    but fail this one (``main.py`` would contain literal ``{{
    project_name }}``).

    Asserting on the *interpolated* form (``demo``) rather than on the
    *absence* of template syntax is more precise: a broken renderer
    might produce ``{{ project_name }}`` (unrendered) or ``demo``
    (correct) or ``None`` (evaluation bug), and only one of those is
    correct. The equality assertion distinguishes all three.
    """
    result = _stage(tmp_path)
    try:
        rendered_main = (result.root / "src" / "demo" / "main.py").read_text(encoding="utf-8")
        assert "demo" in rendered_main, (
            "render-kind file does not contain the interpolated variable — "
            "the engine may be copying it verbatim instead of rendering"
        )
        assert "{{" not in rendered_main, (
            "render-kind file still contains template syntax — the renderer " "was not invoked"
        )
    finally:
        result.destroy()
