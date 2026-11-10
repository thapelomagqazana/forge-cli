"""Criterion 2: unknown filters are rejected; known filters work end-to-end.

The guarantee
-------------
Templates may use **only** the filters listed in
``forge.rendering.filters.ALLOWED_FILTERS``. Anything else must:

1. **Fail loudly** — return ``Err(...)`` with a structured
   ``TemplateRenderError``, not silently produce a wrong value or a
   traceback.

2. **Be actionable** — the error must carry a ``code`` from the
   documented set (``syntax.error`` for Jinja's parser rejection, or
   ``filter.not_allowed`` if a future renderer adds explicit filter
   checks), and a non-empty ``hint`` that points at the allowlist
   docs.

3. **Not be a false positive** — the *absence* of a filter from the
   allowlist must be the only reason it fails. A filter that is on the
   allowlist must work; a filter that is not must fail. A test suite
   that only checks the negative direction would let every filter's
   implementation rot while the contract appeared intact.

Two mechanisms, not one
-----------------------
An earlier version of this file conflated two distinct Jinja
mechanisms under the phrase "Jinja defaults":

    * **Filters** are looked up in ``Environment.filters`` and applied
      with the ``|`` operator: ``{{ x | random }}``. An unknown filter
      is a *parse-time* error; Jinja raises ``TemplateAssertionError``
      (a ``TemplateSyntaxError`` subclass) because the template cannot
      be compiled without a filter of that name.

    * **Globals** are looked up in ``Environment.globals`` and called
      as bare names: ``{{ now() }}``, ``{{ lipsum(3) }}``. An unknown
      global is a *render-time* error; Jinja resolves the name to
      ``Undefined`` (or ``StrictUndefined`` here), and the failure
      surfaces as ``UndefinedError`` when the value is used.

These two mechanisms have different failure codes because they
represent different problems for the template author:

    * ``syntax.error``          — "you used a filter name that is not on
                                   the allowlist; check the docs"
    * ``undefined.variable``    — "you referenced a name that is not in
                                   your blueprint's variables; add it,
                                   or use |default"

Conflating the two mechanisms into a single test produced five false
failures (the "globals" cases were asserting ``syntax.error`` when the
renderer correctly emitted ``undefined.variable``). The fix is to test
each mechanism against its own failure code, in its own test. The
guarantee being tested is the same — "stock Jinja capabilities are
cleared from the environment" — but the assertion differs because the
*mechanism of clearance* differs.

What "the allowlist works" means, in three distinct claims
----------------------------------------------------------
The filter allowlist has three independently-verifiable parts:

A. **The set is what the docs say it is.**
   ``test_allowed_filters_is_non_empty`` and
   ``test_allowed_filters_includes_core_names`` verify the *shape* of
   the allowlist. The full doc-sync check (the exact set matches the
   markdown table) lives in ``test_filter_docs.py`` — a maintenance
   check, kept separate because it inspects documentation, not
   behaviour.

B. **Jinja's defaults are cleared.**
   ``test_jinja_default_filters_are_removed`` and
   ``test_jinja_default_globals_are_removed`` verify that stock Jinja
   capabilities are unreachable. Splitting filter from global is
   deliberate — see "Two mechanisms, not one" above.

C. **Every allowlisted filter renders correctly.**
   ``test_allowed_filter_renders_correctly`` exercises each filter
   through the full render pipeline. A filter could be listed in
   ``ALLOWED_FILTERS`` (passing the doc-sync check) but crash when
   called (breaking every blueprint that uses it). This test closes
   that gap.

Test strategy
-------------
- **One claim per test.** Each test asserts one of the three claims
  above, with a name that reads as the claim. A failure message points
  at exactly which part of the contract broke.
- **Parameterised where the cases are symmetrical.** The filter/global
  escape tests and the render test share a shape (one template, one
  expected outcome per case), so ``parametrize`` keeps them compact.
- **Both directions asserted.** "Unknown fails" without "known passes"
  is half a contract; "known passes" without "unknown fails" is the
  other half. Both are here.
- **Mechanism separated from behaviour.** ``test_install_replaces_
  jinja_default_filters`` inspects the environment's filter table
  directly; the render tests exercise it through the public API.
  Testing both is diagnostic, not redundant — a mechanism failure and
  a behavioural failure produce different messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.loading.result import Err, Ok
from forge.rendering import TemplateRenderer
from forge.rendering.filters import ALLOWED_FILTERS

_FIX = Path(__file__).parents[2] / "fixtures" / "templates"


def _render_text(text: str, variables: dict | None = None) -> Ok[str] | Err:
    """Render an inline template string without needing a fixture file.

    The fixture directory is intentionally *not* used for most of these
    tests: a filter test's template is one line, its expected output is
    one line, and a checked-in fixture file would just be indirection.
    The three filter-related fixtures that do exist in
    ``tests/fixtures/templates/`` are used elsewhere (in
    ``test_sandbox_escape.py`` and the determinism tests) where the
    fixture serves as a stable document.

    The file is written to a fresh temp directory per call. The
    directory is not cleaned up by the helper — pytest's own tmp
    management handles that at session end, and the extra allocation is
    negligible for a test suite of this size.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "inline.j2"
    tmp.write_text(text, encoding="utf-8")
    return TemplateRenderer().render(tmp, variables or {})


# ---------------------------------------------------------------------------
# Claim A — the allowlist is present and complete.
# ---------------------------------------------------------------------------
def test_allowed_filters_is_non_empty() -> None:
    """The allowlist is populated — an empty dict would silently pass all tests.

    This is a canary. If a future refactor accidentally cleared
    ``ALLOWED_FILTERS`` (e.g. by importing it before the module
    finished executing, or by a typo in the ``install`` function), the
    "unknown filters fail" tests would all still pass (because *every*
    filter would be unknown), and the "known filters work" tests would
    all fail with a confusing "undefined filter" message. This test
    makes the failure mode legible: the message is "the allowlist is
    empty," not "filter 'upper' not found."

    The assertion is ``>= 10``, not a specific count, so adding filters
    does not require updating this test. The number is chosen to
    catch a catastrophic truncation (like 3 filters surviving) without
    becoming a brittle snapshot of the exact list.
    """
    assert len(ALLOWED_FILTERS) >= 10, (
        f"ALLOWED_FILTERS has only {len(ALLOWED_FILTERS)} entries; "
        "the allowlist may have been accidentally truncated. "
        f"Current contents: {sorted(ALLOWED_FILTERS)}"
    )


def test_allowed_filters_includes_core_names() -> None:
    """The allowlist exposes the filters blueprints rely on.

    A blueprint template that uses ``|upper``, ``|snake``, or
    ``|default`` will fail hard if those filters are ever removed from
    the allowlist. This test names the *core* filters — the ones whose
    absence would break a substantial fraction of blueprints — so a
    well-intentioned cleanup ("we never use ``title``, let's remove
    it") cannot silently break blueprint authors.

    The set is deliberately *not* the full allowlist: adding a filter
    should not require updating this test. The set is the subset that
    any non-trivial blueprint is likely to use.
    """
    core = {
        "upper",
        "lower",
        "title",
        "capitalize",
        "trim",
        "replace",
        "snake",
        "kebab",
        "pascal",
        "default",
        "indent",
        "tojson",
        "length",
        "join",
        "int",
        "string",
    }
    missing = core - set(ALLOWED_FILTERS)
    assert not missing, (
        f"core filters removed from ALLOWED_FILTERS: {sorted(missing)}. "
        "Removing a core filter is a breaking change for blueprint authors "
        "and requires an ADR."
    )


# ---------------------------------------------------------------------------
# Claim B1 — Jinja's default *filters* are cleared.
# ---------------------------------------------------------------------------
# Every name below is a *filter* in stock Jinja — applied with `|`.
# Clearing the filter table must make each one unreachable; Jinja's
# parser then raises `TemplateAssertionError` (a `TemplateSyntaxError`),
# which the renderer maps to `code == "syntax.error"`.
#
# Filters that ship with stock Jinja and must NOT be reachable via `|`:
@pytest.mark.parametrize(
    ("filter_name", "template_snippet"),
    [
        # Non-deterministic — would break byte-identity (ADR-0014).
        ("random", "{{ ['a','b','c'] | random }}"),
        # Parses untrusted content — attack surface.
        ("urlize", "{{ 'http://x' | urlize }}"),
        ("xmlattr", "{{ {'a': 'b'} | xmlattr }}"),
        # Attribute access — contradicts the sandbox's policy.
        ("attr", "{{ {'a': 1} | attr('a') }}"),
        # Iteration with attribute extraction — same policy concern.
        ("map", "{{ [{'x': 1}] | map(attribute='x') }}"),
        ("selectattr", "{{ [{'x': 1}] | selectattr('x') }}"),
    ],
)
def test_jinja_default_filters_are_removed(
    filter_name: str,
    template_snippet: str,
) -> None:
    """A template using a stock-Jinja *filter* Forge denies must fail.

    Filters are looked up in the environment's filter table.
    ``install()`` calls ``environment.filters.clear()`` before
    installing ``ALLOWED_FILTERS``, so any name not on the allowlist is
    a parse-time error: Jinja cannot compile a template that references
    a filter it does not know. The renderer maps the resulting
    ``TemplateAssertionError`` to ``code == "syntax.error"``.

    Why this is separate from ``test_jinja_default_globals_are_removed``:

    Filters and globals are different mechanisms with different failure
    codes, and testing them together (as an earlier version of this
    file did) produces false failures — the globals were being asserted
    against the filter's error code. The two mechanisms get two tests
    so that a leak of one kind is diagnosed separately from a leak of
    the other.

    Asserting on the *code*, not just ``isinstance(result, Err)``:

    A regression that let ``random`` leak back in would produce an
    ``Ok`` (silently non-deterministic output), which
    ``isinstance(result, Err)`` catches. But a regression that mapped
    the filter-unknown error to a *different* code would produce
    ``Err`` with a confusing diagnostic — an error that looks
    actionable but points the user at the wrong thing. Asserting on
    the code ties the test to the contract the CLI consumer relies on.
    """
    result = _render_text(template_snippet)
    assert isinstance(result, Err), (
        f"filter {filter_name!r} should not be available; "
        f"template rendered successfully with output: "
        f"{result.value if isinstance(result, Ok) else '<no value>'!r}"
    )
    assert result.error.code in {"syntax.error", "filter.not_allowed"}, (
        f"unexpected error code for disabled filter {filter_name!r}: "
        f"{result.error.code!r} "
        f"(message: {result.error.message!r})"
    )
    assert result.error.hint.strip(), (
        f"error for disabled filter {filter_name!r} has no hint; "
        "every error must suggest a remediation"
    )


# ---------------------------------------------------------------------------
# Claim B2 — Jinja's default *globals* are cleared.
# ---------------------------------------------------------------------------
# Every name below is a *global* in stock Jinja — called as a bare name,
# not applied with `|`. The environment's globals table is cleared, so
# these names resolve to `StrictUndefined`, which raises on use; the
# renderer maps the resulting `UndefinedError` to
# `code == "undefined.variable"`.
#
# Globals that ship with stock Jinja and must NOT be callable:
@pytest.mark.parametrize(
    ("global_name", "template_snippet"),
    [
        # Non-deterministic — would break byte-identity (ADR-0014).
        ("now", "{{ now() }}"),
        # Stateful — would break determinism under reuse.
        ("cycler", "{{ cycler('a','b').next() }}"),
        ("joiner", "{{ joiner(', ') }}"),
        # Arbitrary-size generation — resource exhaustion.
        ("lipsum", "{{ lipsum(1000) }}"),
        # Mutable-object creation — expands the sandbox's threat model.
        ("namespace", "{% set ns = namespace() %}{{ ns }}"),
        # Convenience constructors — attack surface, and forbid-dict
        # policy means we don't want templates building them anyway.
        ("dict", "{{ dict(a=1) }}"),
        ("range", "{{ range(10) }}"),
    ],
)
def test_jinja_default_globals_are_removed(
    global_name: str,
    template_snippet: str,
) -> None:
    """A template using a stock-Jinja *global* Forge denies must fail.

    Globals live in the environment's globals table (which
    ``globals.py`` clears to empty). A missing global is not a syntax
    error: Jinja resolves the bare name to ``StrictUndefined``, and
    calling it raises ``UndefinedError``. The renderer maps that to
    ``code == "undefined.variable"``, with the offending global name
    extracted into ``name``.

    Why ``range`` and ``dict`` are included even though they are
    arguably harmless:

    The allowlist principle is *"if it is not in the allowlist, it is
    not available."* ``range`` in a template encourages loop bounds
    that come from template logic rather than declared variables,
    which defeats the "inputs come from the validated context"
    property established by the resolver. ``dict`` lets templates
    construct objects whose attributes can be probed, expanding the
    sandbox's threat model. Neither is catastrophic on its own, but
    both are exactly the kind of convenience that erodes an allowlist
    one "we should really allow X" at a time. The test asserts they
    are gone so the erosion cannot happen silently.

    Asserting on ``name``, not just ``code``:

    The renderer's error names the specific missing global. A user
    reading "undefined variable: lipsum" can grep for ``lipsum`` and
    find the offending line; a message that only said "some variable
    is undefined" would leave them searching. Asserting on the
    extracted name pins down that usability contract.
    """
    result = _render_text(template_snippet)
    assert isinstance(result, Err), (
        f"global {global_name!r} should not be available; "
        f"template rendered successfully with output: "
        f"{result.value if isinstance(result, Ok) else '<no value>'!r}"
    )
    assert result.error.code == "undefined.variable", (
        f"unexpected error code for disabled global {global_name!r}: "
        f"{result.error.code!r} "
        f"(message: {result.error.message!r})"
    )
    assert result.error.name == global_name, (
        f"error should name the offending global {global_name!r}, "
        f"got name={result.error.name!r}"
    )
    assert result.error.hint.strip(), (
        f"error for disabled global {global_name!r} has no hint; "
        "every error must suggest a remediation"
    )


# ---------------------------------------------------------------------------
# Claim C — every allowlisted filter renders correctly.
# ---------------------------------------------------------------------------
# One row per allowlisted filter, with a template snippet and its
# expected output. Adding a new filter requires adding a row here —
# that is the point: it forces the author to prove the filter works
# end-to-end, not just that the name is in the dict.
#
# The snippets use literal inputs (not ``{{ variables }}``) so the
# test does not depend on fixture files and reads like a documentation
# table. A reviewer scrolling the test can see every filter's
# behaviour at a glance.
@pytest.mark.parametrize(
    ("filter_name", "template_snippet", "expected"),
    [
        # Case
        ("upper", "{{ 'abc' | upper }}", "ABC"),
        ("lower", "{{ 'ABC' | lower }}", "abc"),
        ("title", "{{ 'hello world' | title }}", "Hello World"),
        ("capitalize", "{{ 'hello' | capitalize }}", "Hello"),
        # Whitespace / simple string ops
        ("trim", "{{ '  hi  ' | trim }}", "hi"),
        ("replace", "{{ 'a-b-c' | replace('-', '_') }}", "a_b_c"),
        # Identifier casing
        ("snake", "{{ 'MyThing' | snake }}", "my_thing"),
        ("kebab", "{{ 'my_thing' | kebab }}", "my-thing"),
        ("pascal", "{{ 'my_thing' | pascal }}", "MyThing"),
        # Defaulting and indentation
        ("default", "{{ missing | default('fallback') }}", "fallback"),
        ("indent", "{{ 'a\\nb' | indent(2) }}", "a\n  b"),
        # Serialisation / structural
        ("tojson", "{{ {'b':2,'a':1} | tojson }}", '{"a":1,"b":2}'),
        ("length", "{{ 'hello' | length }}", "5"),
        ("join", "{{ ['a','b'] | join('-') }}", "a-b"),
        ("int", "{{ '42' | int }}", "42"),
        ("string", "{{ 42 | string }}", "42"),
    ],
)
def test_allowed_filter_renders_correctly(
    filter_name: str,
    template_snippet: str,
    expected: str,
) -> None:
    """Every allowlisted filter produces its documented output.

    This test is the "known filters work" half of the contract. It is
    the counterpart to the "defaults are removed" pair above. Without
    it, a filter could be present in ``ALLOWED_FILTERS`` — passing the
    doc-sync check — but crash when rendered, breaking every blueprint
    that uses it. This test would catch that.

    Each row is one filter's end-to-end proof:
    template → parse → filter lookup → filter execution → render →
    output. If any step is broken, this test fails with the exact
    template snippet and expected output, so a reviewer sees the
    reproduction case directly in the failure message.

    The expected outputs are asserted as exact strings, not substrings.
    A filter that produced ``"ABC "`` (trailing space) would fail the
    ``upper`` case even though the visible behaviour looks correct —
    which is what we want, because trailing whitespace in generated
    code is a real bug (it breaks linters, formatters, and snapshot
    tests downstream).

    The ``default`` row is worth highlighting: it renders
    ``{{ missing | default('fallback') }}`` with an *empty* context,
    which exercises the ``StrictUndefined`` interaction documented in
    ``filters._default``. The filter's contract requires an
    ``isinstance(value, Undefined)`` check *before* the ``value == ""``
    comparison; without that ordering, this test would fail with
    ``UndefinedError: 'missing' is undefined`` — the exact bug this
    test guards against reintroduction of.

    The ``tojson`` row is worth highlighting: it asserts sorted keys
    (``{"a":1,"b":2}`` even though the dict literal was ``{'b':2,'a':1}``).
    This is the determinism contract — same input, same bytes — and
    it makes ``tojson`` output stable across runs regardless of
    insertion order in the caller's dict.

    The ``indent`` row's expected output uses an actual newline
    (``"a\\n  b"`` in the template source becomes ``"a<newline>  b"``
    in the rendered result). The ``splitlines(keepends=True)``
    implementation in ``filters._indent`` preserves the newline and
    indents the second line by two spaces, which is the standard
    Jinja ``indent`` contract.
    """
    result = _render_text(template_snippet)
    assert isinstance(result, Ok), (
        f"filter {filter_name!r} failed to render. "
        f"Template: {template_snippet!r}. "
        f"Error: {result.error.format() if isinstance(result, Err) else 'n/a'}"
    )
    assert result.value == expected, (
        f"filter {filter_name!r} produced wrong output. "
        f"Template: {template_snippet!r}. "
        f"Expected: {expected!r}. "
        f"Got: {result.value!r}."
    )


# ---------------------------------------------------------------------------
# Behavioural guards beyond the three canonical claims.
# ---------------------------------------------------------------------------
def test_unknown_filter_reports_the_filter_name() -> None:
    """The error for an unknown filter names the filter.

    Jinja's parser reports the unknown filter's name in the exception
    message (``No filter named 'nonexistent_filter'.``), and the
    renderer passes the message through verbatim. This test asserts
    that the *rendered* error carries the name, so a user sees
    "nonexistent_filter" in the output and can fix the typo without
    re-running the CLI to get more information.

    This is a *usability* guarantee, not a security one. It is
    tested here because the filter name's presence in the message is
    a property of the filter subsystem, not of the renderer's error
    formatting.
    """
    result = _render_text("{{ 'x' | nonexistent_filter }}")
    assert isinstance(result, Err)
    # The name appears somewhere in either the message or the hint.
    rendered_text = result.error.message + " " + result.error.hint
    assert "nonexistent_filter" in rendered_text, (
        f"error should name the unknown filter; "
        f"message={result.error.message!r}, hint={result.error.hint!r}"
    )


def test_allowlisted_filter_with_wrong_arguments_fails_cleanly() -> None:
    """A filter called with invalid arguments produces a structured error.

    The renderer's guarantee (documented in ``renderer.py``'s module
    docstring as guarantee 7) is that *every* exception a template can
    cause becomes a structured ``TemplateRenderError``. Before this
    test existed, the ``TypeError``/``ValueError``/``AttributeError``
    class was not caught, and a call like ``|replace('a')`` (missing
    the required ``new`` argument) raised a raw traceback — violating
    the "batch renderers can collect every failure" contract that
    ``Result[str, TemplateRenderError]`` exists to provide.

    This test is the reason that branch exists. It asserts:

    - the result is ``Err`` (not a raised exception),
    - the code is ``filter.invalid_arguments`` (not a generic
      ``syntax.error`` — the fix is different: check the filter's
      signature, not the allowlist),
    - the message and hint are both non-empty (the error is actionable).

    The specific filter (``replace``) is chosen because its signature
    (``(value, old, new, count=-1)``) is trivially malformed — omitting
    ``new`` is unambiguously wrong. A filter like ``default`` accepts
    varargs by design, so it would not fail this way; choosing
    ``replace`` avoids a filter whose contract is intentionally loose.

    The test asserts on the *specific* code, not just "an Err with a
    hint." A regression that mapped this class of failure to
    ``syntax.error`` would pass a looser test but mislead the user —
    the hint on ``syntax.error`` says "check the filter name in the
    allowlist," which is exactly the wrong advice for a filter that
    *is* on the allowlist but was called with bad arguments.
    """
    result = _render_text("{{ 'abc' | replace('a') }}")  # missing `new`
    assert isinstance(result, Err)
    assert result.error.code == "filter.invalid_arguments", (
        f"expected 'filter.invalid_arguments', got "
        f"{result.error.code!r} "
        f"(message: {result.error.message!r})"
    )
    assert result.error.message.strip(), "error must carry a message"
    assert result.error.hint.strip(), "error must carry a hint"


def test_filters_are_case_sensitive() -> None:
    """``Upper`` is not the same filter as ``upper``.

    Filter names are case-sensitive in Jinja. A template using
    ``|Upper`` must fail, not silently fall back to ``|upper``. This
    matters because a case-insensitive implementation would turn typos
    into surprises ("I wrote ``|UPPER``, it worked, so I assumed it was
    a standard alias — but my reviewer's linter disagrees").

    The test is here rather than in a "Jinja behaviour" file because
    the *allowlist* is what determines which names are accepted, and
    case sensitivity is a property of that allowlist. If a future
    refactor lowercased the keys (a tempting "make it friendlier"
    change), this test would catch it with a name that explains why
    the change is wrong.
    """
    result = _render_text("{{ 'abc' | Upper }}")
    assert isinstance(result, Err)


def test_default_does_not_fire_for_falsy_values() -> None:
    """``|default`` substitutes only for ``Undefined``, ``None``, ``""``.

    Jinja's stock ``default`` has a "boolean mode" (``|default(x,
    true)``) that also substitutes for ``0``, ``False``, and empty
    containers. That mode is a silent-footgun (a variable legitimately
    set to ``0`` becomes the default), so ``filters._default``
    deliberately ignores the second argument. This test asserts that
    contract: a ``0`` value renders as ``"0"``, not as the fallback.

    Why this is in the filter test file:

    It is the *most likely* filter behaviour to be accidentally
    changed by a well-meaning refactor. Someone reading the code might
    think "the second argument is ignored — that's a bug, let's
    support boolean mode." This test's name and docstring explain why
    it is not a bug, and its failure message would surface the exact
    case that broke.
    """
    result = _render_text("{{ zero | default('fallback') }}", {"zero": 0})
    assert isinstance(result, Ok)
    assert result.value == "0", (
        "|default must not substitute for the integer 0; "
        "boolean mode is intentionally not supported (see filters._default docstring)"
    )


def test_default_fires_for_empty_string() -> None:
    """``|default`` substitutes for ``""`` (the "explicitly blank" case).

    An empty string in a blueprint variable is almost always "the user
    cleared the value in the form," not "the user meant an empty
    string." Substituting the fallback is the least-surprising
    behaviour. This is distinct from ``0`` and ``False``, which are
    legitimate values (``test_default_does_not_fire_for_falsy_values``
    covers that side).

    Both tests exist because the *distinction* is the contract. A
    single test asserting "default works" would pass for an
    implementation that substituted for every falsy value, which is
    precisely what we do not want.
    """
    result = _render_text("{{ empty | default('fallback') }}", {"empty": ""})
    assert isinstance(result, Ok)
    assert result.value == "fallback"


def test_default_fires_for_explicit_none() -> None:
    """``|default`` substitutes for ``None`` (the "not set" case).

    A ``None`` value from a blueprint variable typically means "the
    caller did not set this" (e.g. an optional form field). Substituting
    the fallback is the intended behaviour for that case. This is
    distinct from "not provided at all" (an ``Undefined``, covered by
    the ``default`` row in ``test_allowed_filter_renders_correctly``)
    and from "explicitly 0" (covered by
    ``test_default_does_not_fire_for_falsy_values``).

    Three cases, three tests. Each one would fail for a *different*
    wrong implementation of ``_default``, which is why they are
    separate tests and not one parameterised test.
    """
    result = _render_text("{{ missing | default('fallback') }}", {"missing": None})
    assert isinstance(result, Ok)
    assert result.value == "fallback"


def test_install_replaces_jinja_default_filters() -> None:
    """``install()`` clears Jinja's defaults before adding the allowlist.

    This is the *mechanism* test — the counterpart to the two
    *behavioural* tests (``test_jinja_default_filters_are_removed`` and
    ``test_jinja_default_globals_are_removed``). Testing both is
    diagnostic, not redundant:

    - The behavioural tests prove the leak is observable through the
      public API (rendering a template). If a future Jinja release
      changed how filters are inherited, they would still pass.
    - This test proves the *mechanism* (``clear()`` then ``update()``)
      is working as intended. If a future change removed the
      ``clear()`` call, the behavioural tests would fail with a
      specific filter name, and this test would fail with the full set
      of leaked filter names — the two failures together are more
      diagnostic than either alone.

    The check is a set operation, not an enumeration: it asserts that
    no stock-Jinja filter is present in the environment's filters map.
    This is stronger than "the allowlist filters are present" (which
    would pass even if defaults leaked through).

    ``now``, ``cycler``, ``joiner``, ``lipsum``, ``namespace``, and
    ``dict`` appear in the leaked-set check but *not* in the
    filter-table allowlist check, because they are globals, not
    filters. They are included here because `install()` is the *only*
    path by which *either* mechanism could leak — the check is
    unified at the mechanism level even though the behavioural tests
    are split at the mechanism level. That asymmetry is intentional:
    the mechanism is one call, the behaviour is two.
    """
    from forge.rendering.environment import get_environment

    env = get_environment()
    installed_filters = set(env.filters)
    installed_globals = set(env.globals)

    # Stock-Jinja *filters* that must NOT be present. A representative
    # sample, not the full list — the full list would be a Jinja
    # snapshot, and this test's job is to catch leaks, not enumerate
    # every default.
    jinja_default_filters = {
        "random",
        "urlize",
        "xmlattr",
        "attr",
        "map",
        "select",
        "reject",
        "selectattr",
    }
    leaked_filters = installed_filters & jinja_default_filters
    assert not leaked_filters, (
        f"Jinja default filters leaked into the Forge environment: "
        f"{sorted(leaked_filters)}. install() must clear() before update()."
    )

    # Stock-Jinja *globals* that must NOT be present. Same rationale
    # as above, applied to the other table.
    jinja_default_globals = {
        "now",
        "cycler",
        "joiner",
        "lipsum",
        "namespace",
        "dict",
        "range",
    }
    leaked_globals = installed_globals & jinja_default_globals
    assert not leaked_globals, (
        f"Jinja default globals leaked into the Forge environment: "
        f"{sorted(leaked_globals)}. globals.install() must clear() "
        f"before update()."
    )

    # And the allowlist is present, so the test cannot pass by accident
    # on a completely empty filters map.
    missing_filters = set(ALLOWED_FILTERS) - installed_filters
    assert not missing_filters, (
        f"allowlisted filters missing from the environment: "
        f"{sorted(missing_filters)}. install() did not run."
    )
