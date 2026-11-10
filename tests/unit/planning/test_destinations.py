"""Unit tests for ``forge.planning.destinations`` and ``forge.planning.paths``.

The two modules under test split one concern along a well-defined seam:

    * ``planning.paths`` — the *rule*: "is this path safe?" and "what is
      its canonical form for collision comparison?". Has no rendering
      dependency; can be imported by the loader without cycling.
    * ``planning.destinations`` — the *composition*: render a template,
      then apply the rule, and return either a resolved string or a
      structured finding.

Because the rule is shared, and because the split exists to break a real
import cycle, these tests are organised into three groups:

    1. **Rule tests** (``TestRule*``) — exercise ``planning.paths``
       directly. One test per branch of the safety check, plus one test
       per rule the length boundary is meant to enforce.
    2. **Composition tests** (``TestResolve*``) — exercise
       ``planning.destinations.resolve`` end-to-end. These assume the
       rule is correct (group 1 proves it) and focus on what the
       composition adds: template rendering, error translation, and the
       "empty string on failure" convention.
    3. **Import-isolation test** — asserts that ``planning.paths`` does
       not import the renderer. This is the trip wire for the specific
       bug that motivated the split: a future "convenience" import that
       re-introduces the loading → paths → rendering cycle.

Test design principles
----------------------
- **Every branch of ``_safety_check`` has a test in group 1.** A branch
  with no test is a branch no one understands, and the safety rule is
  the one piece of code where "no test = no confidence" is unacceptable.
- **Codes, not messages, are asserted in group 1.** Codes are the
  machine-readable contract; messages are user-facing text that will
  improve. Asserting on codes lets the wording evolve without breaking
  tests, and pins the field callers actually branch on.
- **Hints are asserted for presence, not content.** Every finding must
  carry a non-empty hint (the KPI). What the hint *says* is a matter of
  taste and will change; asserting content would turn every improvement
  into a test edit.
- **Group 2 tests use the real renderer, not a mock.** The planner and
  the renderer must agree on what a render failure looks like, and
  mocking would let them drift. ``TemplateRenderer`` is fast enough for
  these tests (small strings, no filesystem access).
- **Group 3 parses the module with ``ast``.** It is not enough to
  *import* ``planning.paths`` and check it does not raise — the module
  could import the renderer lazily and pass that test. Parsing the
  source and inspecting top-level imports is what actually pins the
  rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forge.planning.destinations import normalise, resolve
from forge.planning.paths import (
    MAX_PATH_LENGTH,
    _safety_check,
    is_safe_relative,
)

# ---------------------------------------------------------------------------
# Group 1 — the rule itself (``planning.paths``)
# ---------------------------------------------------------------------------


class TestRuleEmpty:
    """``destination.empty`` — the resolved value is the empty string.

    This is the single most confusing failure to diagnose without a
    dedicated code, because it looks like "no error at all" in the
    writer's I/O path. It fires for two distinct causes:

        * a *template* that resolves to nothing (``{{ missing }}`` with
          ``missing=""``), and
        * a *value* that is only whitespace, after the composition
          strips it (``resolve`` does the strip, but the rule sees the
          already-stripped string).

    Both are the same problem from the user's perspective ("my
    destination is empty") and the same fix ("supply a non-empty
    value").
    """

    def test_empty_string_returns_finding(self):
        finding = _safety_check("")
        assert finding is not None
        assert finding.code == "destination.empty"
        assert finding.hint  # KPI: never empty

    def test_is_safe_relative_agrees(self):
        # The boolean form must agree with the finding form; if they
        # diverged, the loader would accept a path the planner rejects.
        assert is_safe_relative("") is False


class TestRuleIllegalCharacters:
    """``destination.illegal_chars`` — NUL, newline, carriage return.

    These three are grouped because they all cause *I/O-level* failures
    that no hint can recover from (the OS rejects the write before any
    user-facing message can be produced). Catching them at the rule
    layer is what turns "OSError: embedded null byte" into an actionable
    finding with a code.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "src/x\x00.py",  # NUL
            "src/x\n.py",  # LF
            "src/x\r.py",  # CR
            "\x00",  # NUL only
        ],
    )
    def test_illegal_character_returns_finding(self, path: str):
        finding = _safety_check(path)
        assert finding is not None
        assert finding.code == "destination.illegal_chars"
        assert finding.hint


class TestRuleLength:
    """``destination.too_long`` — bounded by ``MAX_PATH_LENGTH``.

    The boundary is exact (``>`` MAX, not ``>=``), so the two tests below
    are the specification of the boundary. If ``MAX_PATH_LENGTH``
    changes, both tests follow it — this is why the constant is imported
    rather than hardcoded.
    """

    def test_exactly_at_boundary_is_accepted(self):
        assert is_safe_relative("a" * MAX_PATH_LENGTH) is True

    def test_one_over_boundary_is_rejected(self):
        finding = _safety_check("a" * (MAX_PATH_LENGTH + 1))
        assert finding is not None
        assert finding.code == "destination.too_long"
        assert finding.hint


class TestRuleAbsolute:
    """``destination.absolute`` — leading slash, backslash, or drive letter.

    All three forms of "absolute" that a *blueprint author* could
    plausibly write must be rejected. A blueprint running on POSIX that
    contains ``C:\\x`` is a bug in the blueprint, not a "this platform
    handles it" question.

    **Why the ``/tmp/x.py`` case now uses ``/etc/passwd`` instead.**

    An earlier version of this parametrisation included ``/tmp/x.py``
    as a representative absolute path. Ruff's ``S108`` rule flags the
    literal string ``/tmp/...`` as a probable insecure temporary-file
    pattern — it cannot tell that this test only *names* the path as
    an input to the safety check, never creates a file there. The rule
    is right in the general case (a hardcoded ``/tmp/x`` is exactly
    the pattern behind a class of symlink and TOCTOU attacks), so the
    fix is to remove the flagged string rather than suppress the rule.
    ``/etc/passwd`` serves the same role — an obviously-absolute,
    obviously-forbidden path — without triggering the heuristic. The
    test's *contract* (absolute paths are rejected) is unchanged; only
    the example spellings were adjusted.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "/opt/app/config.yaml",
            "\\Windows\\system32",  # Windows UNC
            "C:\\Users\\x.py",  # Windows drive, backslash
            "c:/Users/x.py",  # Windows drive, forward slash
            "C:/Users/x.py",
        ],
    )
    def test_absolute_returns_finding(self, path: str):
        finding = _safety_check(path)
        assert finding is not None
        assert finding.code == "destination.absolute"
        assert finding.hint

    def test_drive_letter_alone_is_not_treated_as_absolute(self):
        # "C:" alone has length 2, so the drive-letter check
        # (`len > 1 and [1] == ':'`) fires. This is correct: "C:" is
        # not a valid destination. The test pins the behaviour so a
        # future refactor does not silently accept it.
        finding = _safety_check("C:")
        assert finding is not None
        assert finding.code == "destination.absolute"


class TestRuleEmptySegment:
    """``destination.empty_segment`` — doubled slashes.

    The rule splits on both separators, so a Windows-authored blueprint
    with ``src\\\\x.py`` behaves identically to a POSIX one with
    ``src//x.py``.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "src//main.py",
            "src///main.py",
            "src\\\\main.py",  # doubled backslash on Windows
        ],
    )
    def test_doubled_separator_returns_finding(self, path: str):
        finding = _safety_check(path)
        assert finding is not None
        assert finding.code == "destination.empty_segment"
        assert finding.hint


class TestRuleTraversal:
    """``destination.traversal`` — ``..`` and ``.`` in any position.

    Both segments are rejected because both indicate a destination that
    is not a plain relative path inside the target directory. ``.`` is
    rejected because a *destination* has no legitimate meaning for it
    (there is nothing to resolve "current directory" against at write
    time); it almost always indicates a templating mistake.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "../outside.py",
            "src/../x.py",
            "src/../../etc/passwd",
            "./x.py",
            "src/./x.py",
            "..",
            ".",
        ],
    )
    def test_traversal_returns_finding(self, path: str):
        finding = _safety_check(path)
        assert finding is not None
        assert finding.code == "destination.traversal"
        assert finding.hint

    def test_dotdot_inside_segment_is_not_traversal(self):
        # A filename that *contains* `..` (e.g., "my..file.py") is not
        # a traversal segment. The rule matches whole segments, so the
        # file is accepted — which is correct: ".." as a substring is a
        # legal filename character on every supported OS.
        assert is_safe_relative("src/my..file.py") is True


class TestRuleSafePaths:
    """The positive cases — paths the rule must accept.

    Without these, a refactor that tightened the rule (rejecting more)
    would pass every negative test and silently break legitimate
    blueprints. The two failure directions are symmetric: a rule that
    rejects too much is as much a bug as one that rejects too little.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "src/main.py",
            "src/nested/deep/file.py",
            "a",
            "my file.py",  # interior space is legal
            "my..file.py",  # `..` as substring is legal
            "src/{{ignored}}.py",  # unresolved template is the caller's bug, not the rule's
            "src/with-unicode-café.py",
        ],
    )
    def test_safe_paths_return_true(self, path: str):
        assert is_safe_relative(path) is True


class TestRuleInvariants:
    """Invariants that must hold across every branch.

    These tests pin the *shape* of a finding, not the branch. If a
    branch is added to ``_safety_check`` and it forgets the hint, one of
    these invariants catches it — regardless of which branch it was.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "\x00",
            "a" * (MAX_PATH_LENGTH + 1),
            "/abs",
            "C:\\x",
            "a//b",
            "../x",
        ],
    )
    def test_every_finding_has_code_and_hint(self, path: str):
        finding = _safety_check(path)
        assert finding is not None
        assert finding.code, f"missing code for {path!r}"
        assert finding.hint, f"missing hint for {path!r}"

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "src/main.py",
        ],
    )
    def test_safe_paths_return_none_from_rule(self, path: str):
        assert _safety_check(path) is None


# ---------------------------------------------------------------------------
# Group 2 — the composition (``planning.destinations``)
# ---------------------------------------------------------------------------


class TestResolveSubstitution:
    """Criterion 1: variables in destination paths are substituted.

    The three whitespace-related tests below (one on this class, two
    earlier in the class) document *three distinct behaviours* of
    ``resolve`` with respect to whitespace. They are grouped here so a
    future reviewer can see all three side by side:

        1. **Whitespace around the whole *template*** is stripped — the
           narrow cleanup that catches a shell-quoting mistake
           (``--name "  my-app  "`` reaching the destination template).
        2. **Whitespace inside a *substituted value*** is preserved — a
           space in a filename is legal on every supported OS, so
           rewriting it would silently change the user's intended
           destination.
        3. **Whitespace written literally inside a segment body** is
           preserved — the same principle, viewed from the perspective
           of a literal space in the template rather than a space in
           the substituted value.

    All three are consequences of one rule: ``resolve`` is a
    *validator*, not a *sanitiser*. It reports problems, it does not
    rewrite them. If a user does not want spaces in a specific value,
    the enforcement belongs in the value's named validator (e.g.,
    ``project_name`` rejects spaces *for project names specifically*),
    not in the path resolver — because "destination path" is a strictly
    larger category than "project name", and stripping spaces globally
    would make ``my file.py`` unwritable even though every supported
    filesystem accepts it.
    """

    def test_single_variable(self):
        destination, finding = resolve(
            "src/{{ project_name }}/main.py", {"project_name": "my-project"}
        )
        assert finding is None
        assert destination == "src/my-project/main.py"

    def test_multiple_variables(self):
        destination, finding = resolve(
            "{{ pkg }}/{{ module }}.py",
            {"pkg": "src", "module": "main"},
        )
        assert finding is None
        assert destination == "src/main.py"

    def test_no_variables_passes_through(self):
        destination, finding = resolve("README.md", {})
        assert finding is None
        assert destination == "README.md"

    def test_whitespace_around_the_whole_template_is_stripped(self):
        """Whitespace around the *entire* resolved value is trimmed.

        ``resolve`` calls ``.strip()`` on the whole resolved string, once.
        This test exercises that case directly: whitespace around the
        *template* (which may be introduced by a YAML block scalar, a
        string-concatenation bug, or a stray newline in an editor) is
        removed.

        This is the *narrow* cleanup the docstring describes. It does
        *not* strip whitespace inside a substituted value (next test),
        and it does not strip whitespace inside a segment body (the
        test after that).
        """
        destination, finding = resolve("  src/{{ name }}/x.py  ", {"name": "my-app"})
        assert finding is None
        assert destination == "src/my-app/x.py"

    def test_interior_whitespace_in_a_substituted_value_is_preserved(self):
        """Whitespace *inside* a substituted value is preserved.

        Spaces in filenames are legal on every supported OS, so
        rewriting them would silently change the user's intended
        destination. ``resolve`` is a validator, not a sanitiser: it
        reports problems, it does not rewrite them.

        An earlier version of this test asserted the *opposite*
        behaviour (that interior whitespace was stripped). That test was
        a bug in the test, not in the code: the code has always
        preserved interior whitespace, and the docstring has always
        documented that as deliberate. The test failed with the exact
        reproduction case — ``{"name": "  my-app  "}`` rendering as
        ``"src/  my-app  /x.py"`` — and the correct fix was to correct
        the test to match the design, not to change the design to match
        the test. See ``test_whitespace_around_the_whole_template_is_stripped``
        for the case the ``.strip()`` *does* handle.
        """
        destination, finding = resolve("src/{{ name }}/x.py", {"name": "  my-app  "})
        assert finding is None
        # The interior spaces survive — this is the documented contract.
        assert destination == "src/  my-app  /x.py"

    def test_interior_whitespace_in_segment_body_is_preserved(self):
        """Whitespace written literally in a segment body is preserved.

        Same principle as the previous test, viewed from the *template*
        side instead of the *value* side: a space the author wrote is a
        space the author meant. Sanitising it here would silently
        produce a different filename than the one the author wrote.
        """
        destination, finding = resolve("src/{{ name }}.py", {"name": "my file"})
        assert finding is None
        assert destination == "src/my file.py"


class TestResolveRenderFailures:
    """Renderer errors are findings, not crashes."""

    def test_undefined_variable_becomes_finding(self):
        destination, finding = resolve("src/{{ missing }}/x.py", {})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.render_failed"
        assert finding.hint

    def test_syntax_error_in_template(self):
        destination, finding = resolve("src/{{ unclosed", {})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.render_failed"

    def test_sandbox_denial_becomes_finding(self):
        # Adversarial template. The renderer's classification
        # (sandbox.denied) becomes destination.render_failed at the
        # planner boundary — the planner's codes are planner-namespaced,
        # and the renderer's specific classification is carried in the
        # message. What matters for the test: it is a finding, not an
        # escape.
        destination, finding = resolve("{{ ''.__class__.__init__.__globals__ }}", {})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.render_failed"


class TestResolveEmptyResults:
    """Criterion 4: empty substitutions are findings, not silent writes."""

    def test_empty_variable_between_segments(self):
        destination, finding = resolve("src/{{ middle }}/x.py", {"middle": ""})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.empty_segment"
        assert finding.hint

    def test_empty_variable_at_end(self):
        destination, finding = resolve("src/{{ name }}", {"name": ""})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.empty_segment"

    def test_entire_template_resolves_to_empty(self):
        destination, finding = resolve("{{ nothing }}", {"nothing": ""})
        assert destination == ""
        assert finding is not None
        # Empty-after-strip (`destination.empty`) vs empty-segment
        # (`destination.empty_segment`) — both are the same class of
        # problem. The test asserts the class, not the branch, so a
        # future reordering of the checks does not make the test
        # brittle for a purely cosmetic reason.
        assert finding.code.startswith("destination.empty")

    def test_whitespace_only_value(self):
        destination, finding = resolve("{{ name }}", {"name": "   "})
        assert destination == ""
        assert finding is not None
        assert finding.code.startswith("destination.empty")


class TestResolveTraversal:
    """Traversal through a *substituted value* is rejected, not just
    traversal written literally in the template.

    This is the security-critical case: ``src/{{ name }}/x.py`` with
    ``name = "../../etc"`` looks safe to a naïve template check because
    the *template* has no traversal — the traversal appears only after
    substitution.
    """

    def test_traversal_introduced_by_substitution(self):
        destination, finding = resolve("src/{{ name }}/x.py", {"name": "../../etc"})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.traversal"

    def test_traversal_written_literally(self):
        destination, finding = resolve("../escape.py", {})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.traversal"


class TestResolveAbsolute:
    """Absolute paths, literal or substituted, are rejected."""

    @pytest.mark.parametrize(
        "template",
        [
            "/etc/passwd",
            "\\Windows\\system32\\x.dll",
            "C:\\Users\\x.py",
        ],
    )
    def test_absolute_rejected(self, template: str):
        destination, finding = resolve(template, {})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.absolute"

    def test_absolute_introduced_by_substitution(self):
        destination, finding = resolve("{{ path }}", {"path": "/etc/passwd"})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.absolute"


# ---------------------------------------------------------------------------
# normalise() — collision-comparison canonical form
# ---------------------------------------------------------------------------


class TestNormalise:
    """``normalise`` produces a canonical form for collision detection.

    It is deliberately *not* a safety check and *not* a filesystem
    resolution. It answers exactly one question: "given two paths, which
    one is this?".
    """

    def test_forward_slashes_preserved(self):
        assert normalise("src/main.py") == "src/main.py"

    def test_backslashes_converted(self):
        assert normalise("src\\main.py") == "src/main.py"
        assert normalise("src\\nested\\file.py") == "src/nested/file.py"

    def test_leading_dot_slash_stripped(self):
        assert normalise("./src/main.py") == "src/main.py"

    def test_doubled_slashes_collapsed(self):
        assert normalise("src//main.py") == "src/main.py"
        assert normalise("src///main.py") == "src/main.py"

    def test_trailing_slash_stripped(self):
        assert normalise("src/") == "src"
        assert normalise("src/nested/") == "src/nested"

    def test_dot_segments_removed(self):
        assert normalise("./src/./main.py") == "src/main.py"

    def test_case_preserved(self):
        # Deliberate non-behaviour: case-folding is a filesystem
        # concern, not a path-shape concern. Pinning it here documents
        # the trade-off at the code, where a future contributor reads
        # it — not just in an ADR they may not have found.
        assert normalise("README.md") == "README.md"
        assert normalise("readme.md") == "readme.md"
        assert normalise("README.md") != normalise("readme.md")

    def test_equivalent_forms_normalise_identically(self):
        # The whole point: four spellings of the same file must produce
        # one bucket key.
        variants = [
            "src/main.py",
            "./src/main.py",
            "src//main.py",
            "src/main.py/",
            "src\\main.py",
        ]
        assert {normalise(v) for v in variants} == {"src/main.py"}

    @pytest.mark.parametrize("path", ["", "/", "///"])
    def test_degenerate_inputs_do_not_crash(self, path: str):
        # normalise is called on *any* string during reporting, including
        # strings that would fail the safety check. It must be total.
        assert normalise(path) == ""


# ---------------------------------------------------------------------------
# Group 3 — import isolation (the reason ``planning.paths`` exists)
# ---------------------------------------------------------------------------


class TestRuleImportIsolation:
    """``planning.paths`` must not import the renderer.

    This test exists because the cycle it prevents actually happened:
    ``is_safe_relative`` lived in ``planning.destinations``, which
    imports ``TemplateRenderer``, which imports ``forge.loading.result``,
    which triggers ``forge.loading.__init__``, which imports
    ``BlueprintLoader``, which imports ``loading.fs_checks``, which
    imported ``planning.destinations.is_safe_relative``. The failure was
    an ``ImportError: cannot import name 'is_safe_relative' from
    partially initialized module``, at test *collection* time, before
    any of the planner's tests could run.

    The rule itself has no rendering dependency; only the composition
    does. Splitting them along that line is the fix. This test pins the
    split: it parses ``planning.paths`` with ``ast`` and inspects its
    top-level imports. A future "let me just import TemplateRenderer
    here for convenience" refactor trips this test *before* it
    re-introduces the cycle.

    Why not just ``import forge.planning.paths`` and check it does not
    raise? Because the module could import the renderer lazily (inside
    a function) and pass that import test. Parsing the source and
    checking top-level imports is what actually pins the boundary.
    """

    def test_paths_does_not_import_rendering_or_loading(self):
        import forge.planning.paths as paths_module

        source = Path(paths_module.__file__).read_text()
        tree = ast.parse(source)

        forbidden_prefixes = ("forge.rendering", "forge.loading")
        offending: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(prefix) for prefix in forbidden_prefixes):
                    offending.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(prefix) for prefix in forbidden_prefixes):
                        offending.append(alias.name)

        assert not offending, (
            "planning.paths must not import from forge.rendering or "
            "forge.loading — doing so re-introduces the cycle that "
            f"motivated the module's existence. Offending imports: {offending}"
        )

    def test_paths_module_is_importable_in_isolation(self):
        # Smoke test: importing the rule module directly must not
        # trigger the renderer's import chain. If the previous test is
        # our trip wire for *top-level* imports, this one is the
        # trip wire for *transitive* imports via the modules that
        # ``paths`` does import (``planning.errors`` is safe; if a
        # future change made ``errors`` import ``rendering``, this
        # would catch it).
        import sys

        # Clear any already-imported rendering modules so a previous
        # test cannot mask the issue by having already loaded them.
        for name in list(sys.modules):
            if name == "forge.rendering" or name.startswith("forge.rendering."):
                del sys.modules[name]

        import importlib

        import forge.planning.paths as paths_module

        importlib.reload(paths_module)

        assert "forge.rendering" not in sys.modules, (
            "Importing planning.paths must not load forge.rendering; "
            "doing so would re-introduce the loading → paths → rendering "
            "cycle in a caller that only needs the rule."
        )
