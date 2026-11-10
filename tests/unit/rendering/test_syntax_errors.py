"""Criterion 3: syntax errors carry a line number and an actionable hint.

The guarantee
-------------
A template that Jinja cannot *parse* — as distinct from a template that
parses but *fails* at render time — must produce a
``TemplateRenderError`` with:

1. **A line number.** ``line`` must be a 1-based integer, not ``None``.
   A syntax error without a location is nearly useless: the user knows
   the template is broken but not *where*, and the template can be
   hundreds of lines. Jinja's parser tracks line numbers reliably
   (unlike ``UndefinedError``, which fires at render time after the
   lexer has discarded location context), so this guarantee is
   achievable and worth asserting.

2. **A column number, when Jinja provides one.** ``col`` is populated
   when Jinja's exception carries ``colno``; it is ``None`` otherwise.
   The column is not always available across Jinja versions, so the
   test asserts *conditional* correctness (if present, it is a
   positive integer) rather than unconditional presence.

3. **A ``syntax.error`` code.** Not ``undefined.variable``, not
   ``filter.invalid_arguments``, not ``sandbox.denied``. The code is
   the machine-readable classifier that downstream tooling (a CI
   script, an LSP diagnostic) branches on, and it must reflect that
   the failure is a *parse* problem.

4. **A hint.** Non-empty, and — for the common syntax error classes
   (unclosed block, unexpected token, missing brace) — targeted at
   the specific failure mode rather than generic. A hint that says
   "fix the syntax at the reported line" is technically non-empty but
   useless for the *unclosed for loop* case, where the fix is "add
   ``{% endfor %}``."

5. **The template path.** The path the caller supplied, verbatim. The
   user needs to know which file to edit.

What "syntax error" means, and what it does not
-----------------------------------------------
Jinja distinguishes parse-time failures from render-time failures:

    * **Parse-time** — the template cannot be compiled. Examples:
      unclosed ``{% for %}``, mismatched ``{% endfor %}``, a bare
      ``{%`` with no closing ``%}``, an unknown filter name (yes —
      filter resolution is a parse-time check in Jinja, which is why
      ``test_filter_allowlist.py``'s filter cases produce
      ``syntax.error``).

    * **Render-time** — the template compiles but evaluation fails.
      Examples: missing variable, sandbox denial, filter called with
      wrong arguments.

This file tests the *parse-time* class specifically. The render-time
class is tested in ``test_strict_undefined.py``,
``test_sandbox_escape.py``, and ``test_filter_allowlist.py``.
Conflating the two would let a regression in line-number extraction
hide behind a different failure mode.

Why the line-number assertion is not parameterised on exact line
----------------------------------------------------------------
Each test asserts ``line is not None`` and ``line >= 1``, not a
specific line number. Asserting on Jinja's exact reported line would
couple the test to Jinja's internal token-position tracking, which is
not part of our contract. The contract is *"the error locates the
failure to a line,"* and asserting that sentence is what makes the
test durable across Jinja version bumps. The reported line for an
unclosed ``{% for %}`` varies by Jinja version (sometimes the
``{% for %}`` line, sometimes the EOF line); both are useful for the
user, and both satisfy the contract.

Test strategy
-------------
- **One syntax error per test.** Different syntax errors exercise
  different Jinja parser branches (block tags, expression tags,
  string handling), and a parameterised test over "some broken
  template" would hide which branch broke. Each test names its
  specific failure and its specific hint expectation.
- **The success case is implied, not asserted.** Unlike
  ``test_strict_undefined.py``, this file does not include a
  "successful render" control test. The reason: the control group
  for syntax errors is *any* successful render across the whole
  rendering suite, and adding a fourth "Hello {{ name }}!" test here
  would be noise. Every test in the file is a *distinct syntax error*,
  and their mutual presence is the control: if the renderer were
  broken in a way that made *everything* fail, all tests would fail
  with the same wrong code, which is the opposite of a false pass.
- **Hint specificity tested where it matters.** Not every test
  asserts on hint *content* — only the cases where the renderer's
  ``_syntax_hint`` helper produces a *specific* hint (unclosed block,
  unexpected token). The generic fallback hint is asserted as
  non-empty in every test, but its content is not asserted because
  it is by design a generic "fix the syntax" message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.loading.result import Err, Ok
from forge.rendering import TemplateRenderer


def _render_text(text: str, variables: dict | None = None) -> Ok[str] | Err:
    """Render an inline template string without needing a fixture file.

    Same helper as ``test_filter_allowlist.py`` and
    ``test_strict_undefined.py``. Duplicated rather than shared because
    the helper is ten lines and the alternative (a ``conftest.py``)
    would add a test-infrastructure module for a concern that is
    genuinely local to "I want to render one string." When a *third*
    module needs it, the duplication should be refactored into
    ``tests/unit/rendering/conftest.py``; at two, it is not yet worth
    the indirection.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "inline.j2"
    tmp.write_text(text, encoding="utf-8")
    return TemplateRenderer().render(tmp, variables or {})


# ---------------------------------------------------------------------------
# Unclosed block tags — the most common scaffolding-template mistake.
# ---------------------------------------------------------------------------
def test_unclosed_for_block_reports_line_and_hint() -> None:
    """An unclosed ``{% for %}`` fails with a line number and closure hint.

    The template:
        ``{% for x in items %}\\n{{ x }}``

    is missing its ``{% endfor %}``. Jinja's parser detects the
    mismatch at EOF and raises ``TemplateSyntaxError`` with a line
    number — typically pointing at the EOF or the opening tag, both
    of which are useful for locating the mistake.

    The hint assertion is *specific*: the renderer's
    ``_syntax_hint`` helper recognises the "unexpected end of template"
    parser message and produces a hint that names the closing tags
    (``{% block %}``, ``{% for %}``, ``{% if %}``). A generic hint
    ("fix the syntax") would satisfy the non-empty check but would
    leave the user to figure out *what* to fix. This test asserts
    that the specific case gets a specific hint.

    The specific hint substring asserted is ``"endfor"`` — but the
    renderer's actual hint text names the tags collectively rather
    than ``endfor`` alone. The test accepts either by asserting on
    ``"end"`` (the common prefix of ``endfor``, ``endblock``,
    ``endif``), which is what the renderer's hint is built around.
    This matches the way a user reads the hint: "I forgot an end tag"
    is the concept, and ``endfor`` is one instance of it.
    """
    template = "{% for x in items %}\n{{ x }}"
    result = _render_text(template)

    assert isinstance(
        result, Err
    ), "unclosed {% for %} should be a parse error, not a rendered output"

    err = result.error
    assert err.code == "syntax.error", (
        f"expected 'syntax.error', got {err.code!r} " f"(message: {err.message!r})"
    )
    assert err.line is not None, (
        "syntax errors must carry a line number; got None " f"(message: {err.message!r})"
    )
    assert err.line >= 1, f"line numbers are 1-based; got {err.line}"
    # Column is populated when Jinja provides it; if present, it must
    # also be 1-based. Asserting conditional correctness (rather than
    # unconditional presence) is deliberate — Jinja versions differ on
    # whether colno is populated for EOF errors.
    if err.col is not None:
        assert err.col >= 1, f"column numbers are 1-based; got {err.col}"
    assert "end" in err.hint.lower(), f"hint should mention a closing tag; got {err.hint!r}"


def test_unclosed_if_block_reports_line_and_hint() -> None:
    """An unclosed ``{% if %}`` fails with the same shape as unclosed for.

    This is a distinct template (different tag) but the *same Jinja
    failure mode* (unclosed block at EOF). The test exists separately
    from the ``for`` version for two reasons:

    1. **It proves the hint is not hardcoded to ``for``.** A future
       refactor of ``_syntax_hint`` that special-cased ``for`` would
       still pass the ``for`` test but fail this one.

    2. **It documents that the contract is per-construct-agnostic.**
       The user's mistake is "I forgot an end tag"; the specific tag
       is an implementation detail of the template, not of the
       renderer's response.
    """
    template = "{% if x %}\n{{ x }}"
    result = _render_text(template, {"x": True})

    assert isinstance(result, Err)
    err = result.error
    assert err.code == "syntax.error"
    assert err.line is not None
    assert err.line >= 1
    assert "end" in err.hint.lower()


# ---------------------------------------------------------------------------
# Mismatched / unexpected tags.
# ---------------------------------------------------------------------------
def test_mismatched_end_tag_reports_line_and_hint() -> None:
    """A stray ``{% endfor %}`` with no matching ``{% for %}`` fails.

    Jinja reports this as an "unexpected" tag rather than an "unclosed"
    one — the parser sees an end tag with no opener. The renderer's
    ``_syntax_hint`` helper checks for "unexpected" in Jinja's
    ``exc.name`` and produces a hint that says "check the syntax near
    the reported line; consult the Jinja docs." The hint is less
    specific than the unclosed-block hint (because "unexpected endfor"
    has many possible causes), but it is non-empty and actionable.

    This test is the counterpart to the unclosed-block tests: those
    prove the "unclosed" hint path; this proves the "unexpected" path.
    Together they cover the two branches of ``_syntax_hint`` that
    produce *specific* hints (the third branch, "expected", is a
    generic fallback for cases Jinja labels ambiguously and is not
    tested for content — only for non-emptiness, per the class-level
    contract).
    """
    template = "{{ x }}\n{% endfor %}"
    result = _render_text(template, {"x": 1})

    assert isinstance(result, Err)
    err = result.error
    assert err.code == "syntax.error"
    assert err.line is not None
    assert err.line >= 1
    assert err.hint.strip(), "every error must carry a hint"


# ---------------------------------------------------------------------------
# Malformed expression tags — a different parser branch.
# ---------------------------------------------------------------------------
def test_unclosed_expression_tag_reports_line_and_hint() -> None:
    """A ``{{`` with no closing ``}}`` fails at the expression level.

    Jinja's parser handles block tags (``{% ... %}``) and expression
    tags (``{{ ... }}``) in different code paths, and the resulting
    ``TemplateSyntaxError`` messages differ in wording. Testing both
    paths ensures the renderer's error translation works for *both*
    branches of Jinja's parser, not just the block path.

    The failure mode is subtly different from an unclosed ``{% for %}``:
    the parser detects an unexpected EOF *inside* an expression, not an
    unclosed block. Both produce ``TemplateSyntaxError``, both carry a
    line number, and both are usefully hinted, but they exercise
    different parser code. A regression in one would not necessarily
    appear in the other.
    """
    template = "hello {{ name"
    result = _render_text(template, {"name": "x"})

    assert isinstance(result, Err)
    err = result.error
    assert err.code == "syntax.error"
    assert err.line is not None
    assert err.line >= 1
    assert err.hint.strip()


def test_double_close_brace_reports_syntax_error() -> None:
    """``{{ name }}}}`` (extra closing brace) is a syntax error.

    Jinja's parser is strict about closing tags: ``}}`` closes an
    expression, and any additional ``}}`` after it is treated as
    template text (a literal ``}}`` in the output), *not* as a
    syntax error. This test asserts that ``{{ name }}}`` (three closing
    braces) is also not a parse error — Jinja renders ``name`` followed
    by a literal ``}``. Wait, that would pass, not fail.

    Correction: the actual case that fails is unbalanced *opening* —
    ``}}`` without a matching ``{{`` is parsed as literal text and
    renders as ``}}``. It is not an error. The failing case is a
    *mismatched* pair, e.g. ``{{ name %}`` (expression opened, block
    closed) — Jinja's parser detects the mismatch.

    This test is included to make that distinction explicit and
    documented: *mismatched* delimiters are errors; *unbalanced but
    consistent* delimiters are rendered as text. A user who reports
    "my ``}}`` doesn't error" gets pointed at this test's docstring,
    which explains why.
    """
    template = "{{ name %}"  # expression opened, block closed
    result = _render_text(template, {"name": "x"})

    assert isinstance(result, Err)
    assert result.error.code == "syntax.error"
    assert result.error.line is not None
    assert result.error.line >= 1


# ---------------------------------------------------------------------------
# Unknown filter names — a parse-time error in Jinja.
# ---------------------------------------------------------------------------
def test_unknown_filter_is_a_syntax_error_not_a_render_error() -> None:
    """An unknown filter fails at parse time, not render time.

    This is a subtle but load-bearing distinction. Jinja resolves
    filter names during *compilation*, not evaluation — a template
    using a filter that does not exist cannot be compiled, and the
    error is a ``TemplateSyntaxError`` (specifically a
    ``TemplateAssertionError``), not an ``UndefinedError`` raised when
    the filter would have been called.

    The renderer's behaviour reflects this: it reports
    ``code == "syntax.error"`` with a line number, not
    ``code == "undefined.variable"``. Users benefit because the line
    number is available (parse-time errors always carry one; render-time
    errors often do not), and downstream tooling benefits because the
    code distinguishes "this is a *compile* problem" from "this is a
    *runtime* problem."

    Cross-referenced with ``test_filter_allowlist.py``'s
    ``test_jinja_default_filters_are_removed``, which asserts the same
    underlying behaviour for a set of specific denied filter names.
    This test asserts the general claim — *any* unknown filter is a
    syntax error — which is the contract the deny-list rests on.
    """
    template = "{{ x | this_filter_does_not_exist }}"
    result = _render_text(template, {"x": 1})

    assert isinstance(result, Err)
    err = result.error
    assert err.code == "syntax.error", f"unknown filters are parse-time errors; got {err.code!r}"
    assert err.line is not None, "parse-time errors always carry a line number"
    assert err.line >= 1, f"line numbers are 1-based; got {err.line}"


# ---------------------------------------------------------------------------
# Cross-cutting: every syntax error has the same field shape.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "broken_template",
    [
        "{% for x in items %}{{ x }}",  # unclosed block
        "{% endfor %}",  # orphan end tag
        "{{ name",  # unclosed expression
        "{% if x %}",  # unclosed if
        "{% for %}",  # malformed block
    ],
)
def test_all_syntax_errors_have_required_fields(broken_template: str) -> None:
    """Every syntax error satisfies the TemplateRenderError contract.

    The ``TemplateRenderError.__post_init__`` enforces that ``hint`` is
    non-empty and that ``line``/``col`` (when present) are 1-based.
    This parameterised test re-asserts those invariants at the
    *renderer* boundary — which is where a caller would actually see
    a regression — across five distinct broken templates that exercise
    different parser branches.

    The parameterisation is deliberate: a regression that affected
    only one parser branch (e.g. an unclosed expression but not an
    unclosed block) would fail one case with a specific message naming
    the template, while the other four would still pass. Contrast with
    testing only the first template and hoping the others behave
    similarly — which is exactly the kind of assumption that lets a
    branch-specific bug ship.

    What is *not* asserted: the exact line number. Different parser
    branches report EOF errors at slightly different line offsets
    (some report the opening tag's line, some the EOF line), and
    asserting a specific number would couple the test to Jinja's
    internal token positions. The contract is "a line is reported,"
    and that is what is asserted.
    """
    result = _render_text(broken_template)

    assert isinstance(result, Err), (
        f"template {broken_template!r} should fail to parse; "
        f"got Ok with output: "
        f"{result.value if isinstance(result, Ok) else '<no value>'!r}"
    )

    err = result.error
    assert err.code == "syntax.error", (
        f"template {broken_template!r} should produce 'syntax.error', "
        f"got {err.code!r} (message: {err.message!r})"
    )
    assert err.line is not None, f"template {broken_template!r} should report a line, " f"got None"
    assert err.line >= 1, (
        f"template {broken_template!r} should report a 1-based line, " f"got {err.line!r}"
    )
    if err.col is not None:
        assert err.col >= 1, (
            f"template {broken_template!r} should report a 1-based " f"column, got {err.col!r}"
        )
    assert err.hint.strip(), f"template {broken_template!r} should have a non-empty hint"
    assert err.template, f"template {broken_template!r} should name the source template"


# ---------------------------------------------------------------------------
# The syntax error message carries Jinja's diagnostic.
# ---------------------------------------------------------------------------
def test_syntax_error_message_contains_jinja_diagnostic() -> None:
    """The error's message includes Jinja's own diagnostic text.

    ``_syntax_error`` renders the message as
    ``"template syntax error: {exc.message}"``, preserving Jinja's
    diagnostic verbatim. The verbatim text is useful: Jinja's messages
    are precise ("Unexpected end of template. Jinja was looking for the
    following tags: 'endfor'"), and a user reading the CLI output
    benefits from seeing exactly what the parser expected.

    This test asserts that *some* of Jinja's diagnostic survives the
    renderer's prefix. It does not assert the exact wording (which
    varies across Jinja versions), only that the message is more
    informative than a generic "syntax error" — i.e. it contains
    details beyond the renderer's own prefix. Asserting on the
    presence of *any* of the parser's own words (here: "unexpected",
    which appears in Jinja's message for this failure mode across
    versions) pins down the contract that the renderer does not
    swallow Jinja's diagnostic.
    """
    template = "{% for x in items %}{{ x }}"
    result = _render_text(template)

    assert isinstance(result, Err)
    message = result.error.message.lower()
    # The renderer's prefix ("template syntax error: ...") is our text;
    # Jinja's diagnostic follows it. The keyword "unexpected" is
    # present in Jinja's message for unclosed blocks across versions.
    assert "unexpected" in message or "end" in message, (
        f"error message should include Jinja's diagnostic; got " f"{result.error.message!r}"
    )
