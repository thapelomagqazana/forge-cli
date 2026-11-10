"""Unit tests for literal-copy detection in the file planner.

The planner's job is to decide, for each ``files[]`` entry in a blueprint,
whether the writer should **render** the source as a Jinja2 template or
**copy** it verbatim. Getting this decision wrong in either direction is
a real bug:

* **Rendering a binary** (a PNG, a zip, a font) corrupts it. The bytes
  do not survive a UTF-8 decode, and even if the writer skipped decoding
  for copy-eligible files, treating the file as a template would mean
  searching for ``{{ }}`` in binary data — expensive and pointless.

* **Copying a template** (a ``.j2`` file) leaves ``{{ … }}`` markers in
  the output. That is not a runtime crash, but it is a silent contract
  violation: the user asked for a template and got literal braces.

The rule the planner implements (see ``planning/mapper.py``) is
conservative on purpose:

1. If **either** the source or the destination has a known binary
   extension, the file is copied. This covers two cases:
    - a binary source (``logo.png``) copied to a text-shaped destination
      (``assets/logo`` — unusual, but the destination could legitimately
      drop the extension while the source is unambiguously binary);
    - a template source (``pyproject.toml.j2``) copied to a
      binary-shaped destination (``pyproject.zip`` — unusual, but the
      destination tells the writer what the file *is*).

2. If the source does not end in a template extension
   (``.j2`` / ``.jinja`` / ``.jinja2``), the file is copied. A source
   that is not a template cannot be rendered.

3. Otherwise, the file is rendered.

These tests are the executable specification of that rule. They
parametrise over the extensions the planner knows about, so adding a
new binary extension to ``_BINARY_EXTENSIONS`` or a new template
extension to ``_TEMPLATE_EXTENSIONS`` is a one-line change in
``mapper.py`` plus one new parameter — and the tests fail if a future
refactor silently reorders the rules (say, checks template extension
first and loses the binary-override case).

Test design
-----------
The tests do not exercise the planner end-to-end (that is
``test_planning_end_to_end.py``). They test the *classification* — the
specific rule that decides render-vs-copy — because that is the rule
with the sharp edges and the failure modes that produce corrupt output.
An end-to-end test that happens to hit ``logo.png`` proves the rule
works for ``logo.png``; a parametrised test proves it for every
extension the rule claims to handle, and fails loudly the moment a
future contributor's "one more extension" commit forgets the writer
side of the change.

None of these tests touch the filesystem. The planner's classification
is a pure function of the *strings* (source path, destination path),
so a filesystem is neither required nor useful. Using one would couple
the tests to the host OS's extension quirks (notably macOS's case
folding) for no benefit.
"""

from __future__ import annotations

import pytest

from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    FileEntry,
    VariableType,  # noqa: F401 — imported for the fixture, see below
)
from forge.planning import FileMapper, PlannedFile, PlannedFileKind


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _blueprint_with_files(files: list[FileEntry]) -> Blueprint:
    """Construct a minimal valid ``Blueprint`` declaring ``files``.

    A real ``Blueprint`` instance (not a fake) so the planner validates
    against the actual model contract. The variables list is empty
    because the tests in this module are about *kind classification*,
    not variable substitution — the destination templates used here are
    literal (no ``{{ … }}``), so the resolver's variable pipeline is
    exercised at the level it can be: not at all.

    All fields the planner does not touch are filled with minimal valid
    values. That is intentional: it means a future required field on
    ``Blueprint`` will break this helper in exactly one place, not in
    every test in the file.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.literal",
        name="Literal Copy Demo",
        version="1.0.0",
        description="Fixture for literal-copy classification tests.",
        category=Category.api,
        files=files,
        verification=[
            {"id": "ok", "type": "required_file", "path": "README.md"},
        ],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _plan(*files: FileEntry) -> list[PlannedFile]:
    """Map ``files`` through the planner and return the plan on success.

    Asserting on ``is_ok()`` inside the helper (rather than in every
    test) keeps the tests focused on their assertion. A test that wants
    to exercise the failure path calls ``FileMapper().map`` directly.
    """
    result = FileMapper().map(_blueprint_with_files(list(files)), {})
    assert result.is_ok(), f"planning failed unexpectedly: {result}"
    return result.value


# ---------------------------------------------------------------------------
# Binary extensions are copied, never rendered
# ---------------------------------------------------------------------------
class TestBinaryExtensionsAreCopied:
    """A source with a binary extension is copied even if it *looks* templated.

    The classification rule checks binary extensions *first*, before the
    template-extension check. That order matters: ``logo.png.j2`` has
    both a binary extension (``.png``, embedded) and a template
    extension (``.j2``, at the end). Reversing the order would treat it
    as a template — and a PNG that happens to end in ``.j2`` is almost
    certainly a naming mistake the user needs to fix, not a template to
    render. Copying preserves the bytes; rendering corrupts them.
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "logo.png",
            "banner.jpg",
            "icon.ico",
            "photo.webp",
            "document.pdf",
            "release.zip",
            "archive.tar.gz",
            "bundle.whl",
            "lib.so",
            "app.exe",
            "font.woff2",
            "font.ttf",
            "video.mp4",
            "audio.mp3",
            "cache.sqlite",
            "cache.sqlite3",
            "cache.db",
        ],
    )
    def test_binary_source_is_copied(self, filename: str) -> None:
        plan = _plan(FileEntry(source=f"assets/{filename}", destination=f"out/{filename}"))
        assert len(plan) == 1
        assert plan[0].kind is PlannedFileKind.COPY, (
            f"{filename!r} was classified as RENDER — a binary that "
            f"reaches the renderer will be corrupted."
        )

    def test_binary_source_with_template_destination_is_still_copied(self) -> None:
        """Binary *source* wins over template *destination*.

        A user who writes ``source: logo.png`` and ``destination:
        logo.png.j2`` has almost certainly made a mistake (they will get
        a file called ``logo.png.j2`` in the output), but the planner's
        job is to make the *right* choice given the ambiguous input:
        copying preserves the bytes; rendering would corrupt them. The
        writer or the user must correct the destination — the planner
        must not turn a naming mistake into silent corruption.
        """
        plan = _plan(
            FileEntry(source="logo.png", destination="logo.png.j2"),
        )
        assert plan[0].kind is PlannedFileKind.COPY

    def test_binary_extension_is_case_insensitive(self) -> None:
        """``Logo.PNG`` and ``logo.png`` are the same kind of file.

        Filesystems vary in whether they preserve case on extensions;
        the planner's classification must not. A macOS user with a
        ``Logo.PNG`` asset and a Linux user with ``logo.png`` should get
        the same treatment.
        """
        plan = _plan(FileEntry(source="Logo.PNG", destination="Logo.PNG"))
        assert plan[0].kind is PlannedFileKind.COPY

        plan = _plan(FileEntry(source="logo.Png", destination="logo.Png"))
        assert plan[0].kind is PlannedFileKind.COPY


# ---------------------------------------------------------------------------
# Non-template sources are copied
# ---------------------------------------------------------------------------
class TestNonTemplateSourcesAreCopied:
    """A source without a template extension is a literal, not a template.

    This is the "second rule" of the classification. It is what makes
    a ``README.md`` (a plain Markdown file that the blueprint author
    did not want templated) copy rather than render. The author's
    choice of file extension is the signal: ``.j2`` means "render me",
    anything else means "this is the file I want, byte for byte".
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "README.md",
            "LICENSE",
            "CHANGELOG.md",
            "CONTRIBUTING.rst",
            "data.json",
            "config.toml",
            "config.yaml",
            "config.yml",
            "settings.ini",
            "main.py",  # a .py source is copied, not rendered
            "index.ts",
            "App.tsx",
            "styles.css",
        ],
    )
    def test_non_template_source_is_copied(self, filename: str) -> None:
        plan = _plan(FileEntry(source=f"templates/{filename}", destination=f"out/{filename}"))
        assert len(plan) == 1
        assert plan[0].kind is PlannedFileKind.COPY, (
            f"{filename!r} was classified as RENDER — a source without "
            f"a template extension cannot be rendered, and rendering it "
            f"would search for `{{{{ }}}}` in content the author did not "
            f"mark as a template."
        )


# ---------------------------------------------------------------------------
# Template extensions are rendered
# ---------------------------------------------------------------------------
class TestTemplateExtensionsAreRendered:
    """A source ending in a template extension is rendered.

    This is the "third rule" — the one the user *wants* to fire for
    their actual templates. It is the only case where the writer calls
    the renderer. Getting it wrong (classifying a template as a copy)
    is a soft failure: the output contains literal ``{{ … }}`` markers
    that confuse the user and break the generated project.
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "main.py.j2",
            "pyproject.toml.j2",
            "README.md.j2",
            "Dockerfile.j2",
            "config.yaml.j2",
            "main.py.jinja",
            "main.py.jinja2",
        ],
    )
    def test_template_source_is_rendered(self, filename: str) -> None:
        # Destination drops the template extension (that is the usual
        # pattern: `main.py.j2` renders to `main.py`), so the
        # destination does not trigger the binary check.
        destination = filename.replace(".j2", "").replace(".jinja2", "").replace(".jinja", "")
        plan = _plan(FileEntry(source=f"templates/{filename}", destination=f"out/{destination}"))
        assert len(plan) == 1
        assert plan[0].kind is PlannedFileKind.RENDER, (
            f"{filename!r} was classified as COPY — the output would "
            f"contain literal `{{{{ }}}}` markers instead of the "
            f"resolved values."
        )


# ---------------------------------------------------------------------------
# The rule's edge cases
# ---------------------------------------------------------------------------
class TestClassificationEdgeCases:
    """Boundary cases that a naive implementation would get wrong.

    These are the tests that catch a "clever" refactor. A naive
    classification might use ``str.endswith(binary_extension)`` and
    miss the ``.tar.gz`` case (where the outermost extension is not in
    the binary list); might not preserve the ``.tar.gz`` semantics; or
    might normalise paths (lowercasing) in a way that silently changes
    the destination. Each of these is a real bug, and each test below
    is a trip wire.
    """

    def test_double_extension_binary_is_copied(self) -> None:
        """``archive.tar.gz`` is copied, not rendered.

        ``.tar.gz`` is a compound extension. The classifier must
        recognise either the full compound extension or the outermost
        component (``.gz``) as binary. A naive
        ``filename.endswith(tuple_of_extensions)`` where the tuple
        contains ``.gz`` will match ``.tar.gz`` because ``.gz`` is
        present; the test pins that.

        If a future classifier switches to ``PurePath(filename).suffix``
        (which returns ``.gz`` for ``archive.tar.gz``), the test still
        passes — but the *semantics* of the check have changed, and a
        future test for ``archive.tar.bz2`` would catch a regression
        where ``.bz2`` stopped being recognised.
        """
        plan = _plan(
            FileEntry(source="archive.tar.gz", destination="out/archive.tar.gz"),
        )
        assert plan[0].kind is PlannedFileKind.COPY

    def test_no_extension_is_copied(self) -> None:
        """``LICENSE`` (no extension at all) is copied.

        The source has no extension, so the template-extension check
        fails and the classifier falls through to COPY. That is
        correct: a file with no extension cannot be a template unless
        the blueprint explicitly says so, and "explicit" is a future
        feature (a ``kind: render`` field on ``FileEntry``), not
        something to infer.
        """
        plan = _plan(FileEntry(source="LICENSE", destination="LICENSE"))
        assert plan[0].kind is PlannedFileKind.COPY

    def test_template_named_like_a_binary_is_copied(self) -> None:
        """``fake.png.j2`` — binary-looking *stem*, template extension.

        The stem contains ``.png`` (a binary extension), but the
        *outermost* extension is ``.j2``. Which rule wins?

        Current behaviour: the destination-side check fires first on
        the destination (``fake.png.j2`` if the destination keeps that
        name), so the plan is COPY. That is deliberate: if the *source*
        ends in ``.j2`` but *either* path has a binary extension, the
        file is copied. The reasoning is "when in doubt, do not
        corrupt" — copying a template-shaped file produces literal
        ``{{ }}`` in the output (ugly, fixable), rendering a
        binary-shaped file produces garbage (unrecoverable without the
        original).
        """
        plan = _plan(
            FileEntry(source="fake.png.j2", destination="fake.png"),
        )
        assert plan[0].kind is PlannedFileKind.COPY

    def test_source_and_destination_both_plain_text_are_copied(self) -> None:
        """Both sides have no extensions → COPY.

        A README copied to a README with no extensions on either side
        is a literal copy. The rule has no ambiguity to resolve, and
        the planner should not invent one.
        """
        plan = _plan(FileEntry(source="README", destination="README"))
        assert plan[0].kind is PlannedFileKind.COPY

    def test_extension_matching_is_anchored_at_the_end(self) -> None:
        """A ``.j2`` in the middle of a filename is not a template.

        ``data.j2.json`` has ``.j2`` embedded but ends in ``.json``.
        The classifier must not treat it as a template — the file is
        JSON data (a literal), and rendering it would search for
        ``{{ }}`` in a place that almost certainly has none.

        The test pins that the *outermost* extension (``.json``) is
        what determines template-vs-copy, not a substring match. This
        is the exact bug that substring-based classifiers have, and it
        is a bug the writer would not catch — it would just produce a
        JSON file with literal braces in it.
        """
        plan = _plan(
            FileEntry(source="data.j2.json", destination="data.j2.json"),
        )
        assert plan[0].kind is PlannedFileKind.COPY
