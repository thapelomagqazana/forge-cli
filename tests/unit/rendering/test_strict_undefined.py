"""Criterion 1: missing variables raise, and the error names the variable.

The guarantee
-------------
When a template references a name that is not present in the render
context, the renderer must:

1. **Fail** — return ``Err(...)``, not ``Ok("")``. A silent empty string
   in generated code is the single most common scaffolding bug that
   ships to production: a class with no name, a package with no version.
   ``StrictUndefined`` is what makes this loud.

2. **Name the variable** — the error's ``name`` field must contain the
   exact identifier that was undefined. Not just "something is
   undefined," but "``who`` is undefined," so the user can go fix the
   blueprint's ``variables`` list without re-reading the template.

3. **Point at the file** — the error's ``template`` field must be the
   path the caller supplied, verbatim. Not a temp path, not a
   resolved-to-cwd path. The user needs to see the path *they* wrote
   in their blueprint's ``files[]`` entry, because that is what they
   will go look at.

4. **Carry a hint** — every ``TemplateRenderError`` has a non-empty
   ``hint`` (enforced by ``__post_init__``). This test asserts the
   hint exists *and* suggests a plausible remediation ("Provide a value
   for 'who' in the blueprint's variables..."), because a hint that
   says "try again" would satisfy the type invariant while violating
   the KPI ("100% of template errors are actionable").

Scope note: what "undefined" means, and what it does *not* mean
--------------------------------------------------------------
``StrictUndefined`` treats a name as undefined when it is **not in the
render context at all**. It does *not* treat ``None`` as undefined, and
it does *not* treat a missing attribute on a *defined* object as the
same failure mode as a missing name. Three distinct cases, each with
its own Jinja diagnostic and its own correct assertion:

    ``{{ who }}`` with ``{}``
        → ``'who' is undefined`` — ``name`` = ``"who"``

    ``{{ user.name }}`` with ``{}``
        → ``'user' is undefined`` — ``name`` = ``"user"`` (the *root*,
           because Jinja short-circuited before evaluating the attribute)

    ``{{ user.name }}`` with ``{"user": {}}``
        → ``'dict object' has no attribute 'name'`` — the diagnostic
           names the parent's *type*, not the attribute; ``name`` is
           ``None`` by design (the extraction helper does not parse
           this form — see its docstring)

    ``{{ who }}`` with ``{"who": None}``
        → renders ``"None"`` — *not* an error. ``None`` is a defined
           value; a template that wants a fallback uses ``|default``
           explicitly (and now that the filter is fixed, that works).

The original version of this file conflated the middle two cases (an
early revision assumed Jinja reported ``"user.name"`` as a single
undefined identifier) and asserted the wrong behaviour for the last
case (an early revision assumed ``None`` counted as undefined). Both
assumptions were wrong; both wrong assumptions would have hidden real
behaviour from future maintainers if they had been "fixed" in the
renderer instead of here. The renderer was behaving correctly.

Why this file exists separately from ``test_sandbox_escape.py``
---------------------------------------------------------------
The sandbox test also asserts that some templates produce
``undefined.variable`` errors — but only as a *fallback* code for
escape attempts that StrictUndefined happens to catch before the
sandbox does. That is not the same guarantee as "missing variables
are caught, named, and hinted." Conflating the two would mean a
regression in ``_extract_undefined_name`` could go unnoticed as long
as the escape test still passed for a different reason (e.g. because
``__class__`` was denied by the sandbox before reaching the undefined
path).

This file exercises the undefined path *directly*, with a template
that is not adversarial — it is the ordinary, everyday template a
blueprint author writes. If that fails, the renderer is broken for
every blueprint, not just for hostile ones.

Test strategy
-------------
- **One assertion per guarantee.** Each test targets a single field of
  ``TemplateRenderError`` (``name``, ``template``, ``hint``, ``code``),
  so a failure message points at exactly what broke rather than "the
  error object was wrong."
- **Each undefined case is tested in isolation.** The three distinct
  "not defined" cases (simple name, dotted root, missing attribute)
  have separate tests because they exercise different code paths in
  ``_extract_undefined_name`` and produce different extracted names.
  A parameterised test over all three would need per-case expected
  values, which hides the reasoning; separate tests let each
  docstring explain its own case.
- **The success case is asserted too.** A test file that only asserts
  failures proves the renderer is strict but not that it is *usable*.
  ``test_present_variable_renders_successfully`` is the control group:
  if it fails, the "missing variable" tests would pass for the wrong
  reason (everything is broken, not just the missing-variable path).
- **Explicit-None is documented, not asserted as an error.** The case
  ``{"who": None}`` is a legitimate render (producing ``"None"``), not
  a failure. A test that asserted it *should* fail would be a
  regression in the test, not a bug caught by it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.loading.result import Err, Ok
from forge.rendering import TemplateRenderer

# Fixtures live one level up from `tests/unit/rendering/`.
# `parents[2]` walks rendering -> unit -> tests, then down to
# `fixtures/templates/`.
_FIX = Path(__file__).parents[2] / "fixtures" / "templates"


# ---------------------------------------------------------------------------
# The core guarantee: a missing variable is a failure, not an empty string.
# ---------------------------------------------------------------------------
def test_missing_variable_returns_err_not_empty_string() -> None:
    """The most important assertion in this file.

    A renderer that returns ``Ok("Hello !")`` for a template that
    references ``{{ who }}`` when ``who`` is absent has failed the
    acceptance criterion in the most consequential way: it produced
    output that *looks* fine and silently generated broken code. The
    whole point of ``StrictUndefined`` is that this is an error.

    Asserting ``isinstance(result, Err)`` — not just ``not result.is_ok()``
    — is deliberate: the renderer's contract is ``Result[str,
    TemplateRenderError]``, and the ``Err`` branch is the failure
    shape the caller destructures. A result that is neither ``Ok``
    nor ``Err`` would be a renderer bug, and ``isinstance`` catches it.
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"name": "x"})
    assert isinstance(result, Err)


# ---------------------------------------------------------------------------
# The error identifies *which* variable, not just that something is undefined.
# ---------------------------------------------------------------------------
def test_missing_variable_error_names_the_variable() -> None:
    """The ``name`` field is populated with the exact identifier.

    The fixture ``missing-var.j2`` renders ``{{ who }}``. The context
    supplies ``name`` (a different identifier) so the test is not
    accidentally passing because *both* names happen to extract to
    the same string. If ``_extract_undefined_name`` returned ``"name"``
    for this fixture, the test would fail — proving the assertion is
    meaningful.

    Jinja's message format for a simple undefined variable is stable
    and single-quoted (``'who' is undefined``), which is why the
    extraction helper's regex targets that specific form. If a future
    Jinja release changes the format, ``_extract_undefined_name``
    degrades to returning ``None`` (documented in the helper's
    docstring), and this test catches the degradation by asserting on
    the extracted name rather than the full message.
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"name": "x"})
    assert isinstance(result, Err)
    assert result.error.name == "who"


def test_missing_variable_error_names_the_variable_in_dotted_expression() -> None:
    """``{{ user.name }}`` with ``user`` absent extracts the *root*.

    Jinja evaluates ``user.name`` left-to-right. When ``user`` itself
    is not in the context, the lookup for ``user`` fails before the
    attribute access on ``.name`` is ever attempted — and Jinja's
    error message is ``'user' is undefined``, not ``'user.name' is
    undefined``. This is *correct* Jinja behaviour: it tells the user
    exactly which root name is missing, which is the actionable
    information.

    The extraction helper's regex targets the single-quoted ``'X' is
    undefined`` form, so ``name`` is populated with ``"user"`` — the
    identifier Jinja named, verbatim. Asserting on ``"user"`` rather
    than on ``"user.name"`` reflects this: the *root* is what the
    error must name, because the root is what the user must go add to
    their blueprint's ``variables`` list.

    Why this is *not* asserting on the missing attribute
    ----------------------------------------------------
    The companion case — a *defined* ``user`` (e.g. an empty dict)
    with a missing ``name`` key — produces a different Jinja
    diagnostic (``'dict object' has no attribute 'name'``) and is
    tested separately in
    ``test_missing_attribute_on_defined_parent_does_not_populate_name``.
    Conflating the two cases was the bug in the previous version of
    this file: it assumed the "full dotted path" was always extracted,
    which is only true for the case that never happens (Jinja does not
    defer the root lookup to name the full path — it short-circuits
    the moment the root is undefined).

    The fixture is written inline and deleted afterward because the
    behaviour belongs to ``_extract_undefined_name``, not to the
    rendering pipeline; a checked-in fixture would imply it is shared
    with other tests (it is not) and would accumulate as dead data.
    """
    template = _FIX / "_dotted_missing_root.j2"
    template.write_text("{{ user.name }}\n", encoding="utf-8")
    try:
        result = TemplateRenderer().render(template, {})  # `user` absent
        assert isinstance(result, Err)
        # The root is what Jinja named, and what the user must define.
        assert result.error.name == "user"
    finally:
        template.unlink(missing_ok=True)


def test_missing_attribute_on_defined_parent_does_not_populate_name() -> None:
    """A missing attribute on a *defined* parent has no extracted ``name``.

    When ``user`` is in the context (here as an empty dict) but
    ``name`` is not a key, Jinja raises ``UndefinedError`` with a
    message like ``'dict object' has no attribute 'name'``. Two facts
    make this case distinct from the undefined-root case above:

    1. The message names the parent's *type* (``dict object``), not
       the attribute (``name``) and not the path (``user.name``). This
       is deliberate Jinja behaviour: it tells the user "the object
       you dereferenced does not have that attribute," which is
       exactly the diagnosis they need.

    2. The extraction helper's regex (``'X' is undefined``) does not
       match this form, so ``name`` is ``None``. That is *not* a bug —
       it is the documented behaviour (see the helper's docstring:
       "A future Jinja release that changes wording would cause this
       to return None — the error still includes the full message and
       a generic hint, so the failure mode is *degraded*, not
       *broken*.").

    This test asserts what the renderer actually promises for this
    case: an error with the right ``code``, a non-empty ``message``
    (Jinja's own diagnostic — the actionable part), a non-empty
    ``hint`` (the generic fallback — also actionable, since it
    mentions ``|default``), and a ``name`` of ``None``. Asserting
    ``name == "user.name"`` here would have been the bug: it asked the
    renderer to parse a message it does not parse, for a case where
    the message is already better than a parsed name would be.
    """
    template = _FIX / "_dotted_missing_attr.j2"
    template.write_text("{{ user.name }}\n", encoding="utf-8")
    try:
        result = TemplateRenderer().render(template, {"user": {}})
        assert isinstance(result, Err)
        assert result.error.code == "undefined.variable"
        # The message carries Jinja's diagnostic; it names the type.
        assert result.error.message
        # The hint is the generic fallback — still actionable.
        assert result.error.hint.strip()
        # `name` is deliberately None for this case — see docstring.
        assert result.error.name is None
    finally:
        template.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The error points at the file the caller supplied.
# ---------------------------------------------------------------------------
def test_missing_variable_error_points_at_the_template_path() -> None:
    """``template`` is the path the caller passed, verbatim.

    The renderer receives ``_FIX / "missing-var.j2"``. The error must
    echo that path — not a resolved absolute path, not a temp staging
    path — because the path the caller passed is what the *caller*
    can grep for. If a future change to the renderer started copying
    templates to a temp directory (e.g. to work around a loader
    restriction), the error would point at the temp path and users
    would not be able to find their file.

    Asserting ``in`` rather than ``==`` is deliberate: the renderer is
    allowed to include the path as a substring of a longer label
    (e.g. ``"my-blueprint/templates/missing-var.j2"``), and the test
    should not over-specify the format. What matters is that the
    filename the user wrote is *findable* in the error.
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"name": "x"})
    assert isinstance(result, Err)
    assert "missing-var.j2" in result.error.template


# ---------------------------------------------------------------------------
# The error carries the right machine-readable code.
# ---------------------------------------------------------------------------
def test_missing_variable_error_has_undefined_variable_code() -> None:
    """``code`` is the stable, machine-readable classifier.

    Downstream tooling branches on ``code``, never on the message
    (which may be reworded between releases — see
    ``TemplateRenderError``'s docstring for the codified codes). If a
    future change to the renderer emitted ``type.mismatch`` or
    ``sandbox.denied`` for a genuinely-missing-variable case, the
    error's *message* might still read "undefined variable: who" and
    a message-based test would pass — but any consumer that keyed on
    the code (a CI script, an LSP diagnostic) would silently stop
    working.

    Asserting on ``code`` isolates the contract from the presentation,
    which is the whole reason the field exists.
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"name": "x"})
    assert isinstance(result, Err)
    assert result.error.code == "undefined.variable"


# ---------------------------------------------------------------------------
# The error suggests a fix.
# ---------------------------------------------------------------------------
def test_missing_variable_error_has_actionable_hint() -> None:
    """The hint names the variable and suggests two concrete fixes.

    The ``TemplateRenderError.__post_init__`` already enforces that
    ``hint`` is non-empty, so ``assert result.error.hint`` would pass
    for ``"try again"`` — which is technically a hint but useless.
    This test asserts on the *content*: the hint mentions the
    variable's name (so the user can search for it) and mentions
    ``|default`` (so the user knows the non-destructive alternative to
    declaring a variable).

    The specific wording is not asserted (a rewording should not fail
    the test), only the two substrings that make the hint actionable
    rather than merely present. That is the difference between
    satisfying the type invariant and satisfying the KPI.
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"name": "x"})
    assert isinstance(result, Err)
    hint = result.error.hint
    assert "who" in hint, f"hint should name the variable: {hint!r}"
    assert "default" in hint, f"hint should mention the |default alternative: {hint!r}"


# ---------------------------------------------------------------------------
# The success case is asserted too — this is the control group.
# ---------------------------------------------------------------------------
def test_present_variable_renders_successfully() -> None:
    """Sanity check: the same template renders when the variable is present.

    Without this test, the file would be a collection of failure
    assertions that could all pass while the renderer is completely
    broken (e.g. if every render returned ``Err`` for an unrelated
    reason). The success case proves the failure mode is *specific*
    to missing variables, not a blanket failure.

    What the fixture actually contains
    ----------------------------------
    ``simple.j2`` is exactly ``Hello {{ name }}!\\n`` — **with a
    trailing newline**. The renderer preserves the template's bytes
    faithfully (``keep_trailing_newline=True`` is about *keeping*
    newlines that are there; the environment is also configured with
    ``trim_blocks=False`` and ``lstrip_blocks=False``, so nothing
    strips whitespace on the way out). The assertion below matches
    the fixture byte-for-byte, including the trailing newline.

    An earlier version of this test asserted ``"Hello Ada!"`` (no
    newline), based on a docstring claim that the fixture had no
    trailing newline. That claim was wrong — the file on disk ends
    with ``\\n`` — so the assertion failed against a correct renderer.
    The lesson recorded here: when a test pins an exact string
    against a shared fixture, the fixture's bytes are part of the
    contract, and the docstring describing them must be verified
    against the file, not written from memory.

    The companion test ``test_trailing_newline_preserved`` uses an
    *inline* fixture it creates and controls, so it independently pins
    the newline-preservation behaviour without depending on what
    ``simple.j2`` happens to contain.
    """
    result = TemplateRenderer().render(_FIX / "simple.j2", {"name": "Ada"})
    assert isinstance(result, Ok)
    assert result.value == "Hello Ada!\n"


def test_trailing_newline_preserved() -> None:
    """A template with a trailing newline renders with a trailing newline.

    This is the companion to ``test_present_variable_renders_successfully``.
    That test covers the *content* of a render against a shared fixture;
    this one covers the *whitespace fidelity* of the render against an
    inline fixture the test fully controls. The two properties are
    distinct — a renderer could produce the right content with the
    wrong whitespace (e.g. if ``keep_trailing_newline`` were ``False``,
    or if ``trim_blocks`` were enabled) and the content-only test
    would still pass, or fail for the wrong reason.

    ``keep_trailing_newline=True`` in the environment is what makes
    this pass. Generated code files (Python, YAML) conventionally end
    in a newline, and a renderer that stripped it would break linters,
    diff tools, and ``git``'s "no newline at end of file" warnings.
    Testing it explicitly makes the guarantee visible and independent
    of any shared fixture's bytes.
    """
    template = _FIX / "_trailing_newline.j2"
    template.write_text("Hello {{ name }}!\n", encoding="utf-8")
    try:
        result = TemplateRenderer().render(template, {"name": "Ada"})
        assert isinstance(result, Ok)
        assert result.value == "Hello Ada!\n"
    finally:
        template.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Optional variables via |default do not trip StrictUndefined.
# ---------------------------------------------------------------------------
def test_default_filter_makes_missing_variable_optional() -> None:
    """``{{ who | default("world") }}`` is the documented escape hatch.

    This test exists to prove that ``StrictUndefined`` is not *too*
    strict. If it were, the hint on the undefined-variable error would
    be misleading ("use |default(...)" when |default itself fails).
    The whole point of suggesting |default is that it works.

    The interaction is subtle and worth restating: under
    ``StrictUndefined``, a missing name evaluates to an ``Undefined``
    instance that *raises on any operation*. The ``default`` filter
    must therefore check ``isinstance(value, Undefined)`` **before**
    performing any comparison — otherwise the filter's own ``value ==
    ""`` check would trigger the undefined sentinel's ``__eq__`` and
    raise. This was a real bug in ``_default`` (see
    ``forge/rendering/filters.py`` for the fix and the docstring that
    documents the check ordering).

    This test was failing against the buggy filter, and it is the test
    that *caught* the bug. It stays in the suite because the bug is
    exactly the kind that could be reintroduced by a well-meaning
    refactor ("we don't need the isinstance check, `value == ""`
    handles it") — and if it is reintroduced, this test fails with a
    message that names the interaction.
    """
    template = _FIX / "_default_missing.j2"
    template.write_text('{{ who | default("world") }}\n', encoding="utf-8")
    try:
        result = TemplateRenderer().render(template, {})
        assert isinstance(result, Ok)
        assert result.value == "world\n"
    finally:
        template.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Parameterised check: every *genuinely* undefined failure carries the fields.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("filename", "context", "expected_name"),
    [
        ("missing-var.j2", {"name": "x"}, "who"),
        ("missing-var.j2", {}, "who"),
        # NOT {"who": None}: a None value is *defined* (as None), not
        # undefined. Rendering it produces the string "None", which is
        # correct Jinja behaviour, not a bug — see
        # test_explicit_none_renders_as_string below.
    ],
)
def test_undefined_error_always_has_required_fields(
    filename: str,
    context: dict,
    expected_name: str,
) -> None:
    """Every undefined failure satisfies the TemplateRenderError contract.

    ``TemplateRenderError.__post_init__`` already enforces that
    ``hint`` is non-empty and that ``line``/``col`` are either
    ``None`` or ``>= 1``. This test re-asserts the invariants at the
    *renderer* boundary, which is where a caller would experience a
    regression.

    Two contexts are parameterised, both of which genuinely raise:

    - ``{"name": "x"}`` — the template's variable is missing while a
      *different* variable is present. This is the common case: the
      blueprint author declared some variables but not the one the
      template references.
    - ``{}`` — nothing is provided at all. The caller forgot to pass
      any context. The error must still carry a hint.

    ``expected_name`` makes the parameterisation stricter than a
    boolean check: it asserts on *which* name was extracted, not
    merely that a name was extracted. A regression in
    ``_extract_undefined_name`` that returned an empty string or a
    placeholder would fail here.

    The ``line``/``col`` are deliberately *not* asserted: Jinja does
    not reliably provide line information for undefined errors (the
    failure can occur at render time, after the lexer has discarded
    line tracking). The invariant — that IF they are present they are
    valid — is enforced by the error type's ``__post_init__`` and
    tested separately by ``test_syntax_errors.py``, where line numbers
    are reliably available.
    """
    result = TemplateRenderer().render(_FIX / filename, context)
    assert isinstance(result, Err), f"missing variable should raise for context {context!r}"
    err = result.error
    assert err.code == "undefined.variable"
    assert err.name == expected_name
    assert err.hint.strip(), "every error must carry a hint"
    assert err.template, "every error must name the template"


def test_explicit_none_renders_as_string() -> None:
    """``{"who": None}`` renders the string ``"None"`` — it is not an error.

    A ``None`` value is a *defined* value in Jinja. It is not the same
    as "not provided." The distinction matters because the two cases
    have opposite correct behaviours:

    - **Not provided** (``{}``) — undefined; the renderer raises
      ``undefined.variable``. The user must add a variable to the
      blueprint or use ``|default``.
    - **Provided as None** (``{"who": None}``) — defined; the renderer
      produces ``"None"``. The user is responsible for handling the
      None in the template (e.g. ``{{ who | default("world") }}``
      will substitute the fallback, because the ``default`` filter
      treats ``None`` as absent).

    This is Jinja's contract, and it is the right contract: a
    blueprint variable that is legitimately optional may be passed as
    ``None``, and the template author decides whether that becomes the
    string ``"None"``, an empty string, or a fallback. Silently
    treating ``None`` as undefined would remove that choice.

    The previous version of this test asserted that ``{"who": None}``
    should raise. That was a wrong assumption about the contract, not
    a bug in the renderer — the renderer was correctly producing
    ``"Hello None!\\n"``. Fixing the test to assert the correct
    behaviour is the *only* correct response; "fixing" the renderer to
    satisfy the original assertion would have introduced a real bug.

    The test renders ``missing-var.j2`` with ``{"who": None}`` and
    asserts the full output string, including the fixture's trailing
    newline. Asserting on the full string (rather than on a
    substring) makes the test fail loudly if the renderer ever started
    escaping ``None`` differently (e.g. producing ``"Hello !"`` by
    treating it as falsy — a different plausible wrong implementation).
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"who": None})
    assert isinstance(result, Ok)
    assert result.value == "Hello None!\n"


def test_explicit_none_with_default_filter_substitutes_fallback() -> None:
    """``{{ who | default("world") }}`` with ``{"who": None}`` → ``"world"``.

    This is the *interaction* the two preceding tests document from
    opposite sides: ``None`` is defined (so no undefined error), but
    the ``default`` filter still substitutes for it (so the template
    author's intent — "use a fallback if the value is absent or
    None" — is honoured).

    The filter's contract is: "``Undefined``, ``None``, or ``""`` →
    fallback; everything else → the value." That contract is what
    makes ``{"who": None}`` useful: the blueprint author can declare
    a variable as optional and pass ``None`` from their code, and the
    template transparently substitutes.

    This test exists to make the contract visible. A future change to
    ``_default`` that, say, only substituted for ``Undefined`` (not
    ``None``) would pass the two preceding tests but fail this one —
    and the failure message would name the missing case.
    """
    template = _FIX / "_none_with_default.j2"
    template.write_text('{{ who | default("world") }}\n', encoding="utf-8")
    try:
        result = TemplateRenderer().render(template, {"who": None})
        assert isinstance(result, Ok)
        assert result.value == "world\n"
    finally:
        template.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Guard against the anti-pattern this file exists to prevent.
# ---------------------------------------------------------------------------
def test_no_silent_empty_render_for_missing_variable() -> None:
    """Explicit assertion that the old (silent) behaviour is gone.

    Before ``StrictUndefined`` was enabled, a missing variable would
    render as an empty string, and a template like ``Hello {{ who }}!``
    would produce ``Hello !``. That is a *valid* render of a template
    that *looks* like it succeeded. The historical bug report that
    motivated this guarantee was exactly this: a blueprint author
    shipped a ``class {{ name }}`` template, ``name`` was missing,
    and the generated Python was ``class :`` — syntactically invalid
    Python that failed at *import* time, hundreds of lines away from
    the actual mistake.

    This test is intentionally redundant with
    ``test_missing_variable_returns_err_not_empty_string`` — it asserts
    the same property via a different path (checking the *absence* of
    the silent behavior rather than the *presence* of the error). The
    redundancy is documented, not accidental: if a future maintainer
    is tempted to "make the renderer more forgiving" by catching
    ``UndefinedError`` and returning an empty string, this test fails
    with a message that names the anti-pattern and explains why it is
    forbidden. Reviewers reading the test file learn the lesson; a
    future change cannot silently reintroduce the bug.

    Note the ``.strip()`` in the comparison: the historical silent
    output was ``"Hello !\\n"`` (fixture's trailing newline preserved,
    empty substitution in place of ``who``). ``.strip()`` reduces both
    that and the bare ``"Hello !"`` form to the same string, so the
    guard catches both variants regardless of whether the fixture's
    trailing newline is present.
    """
    result = TemplateRenderer().render(_FIX / "missing-var.j2", {"name": "x"})
    # The line below is intentionally never executed if the renderer
    # is behaving correctly. It exists for its failure message, not
    # for its assertion.
    assert not (isinstance(result, Ok) and result.value.strip() == "Hello !"), (
        "Regression: missing variable rendered silently as empty string. "
        "This is the exact bug StrictUndefined was introduced to prevent "
        "(see ADR-0014 and the renderer module docstring)."
    )
