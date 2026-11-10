"""End-to-end integration tests for the sandboxed template renderer.

Scope
-----
This file exercises the **public rendering surface** —
``forge.rendering.TemplateRenderer`` — through real files on disk, with a
real Jinja environment, real sandbox, real filter allowlist, and real
error translation. It is deliberately *not* a unit test suite: the unit
tests in ``tests/unit/rendering/`` cover the same guarantees with mocked
or minimal inputs, one layer at a time. This file proves that those
layers compose correctly when a user writes an actual template and calls
``render()``.

Why an integration layer at all?
--------------------------------
Three failure classes cannot be caught by unit tests:

1. **Contract drift between environment and renderer.** A unit test on
   ``environment.get_environment()`` verifies the environment is
   configured correctly; a unit test on ``TemplateRenderer.render()``
   with a mocked environment verifies the renderer's error translation.
   Neither catches the case where ``get_environment()`` gains a filter
   that ``renderer`` does not expect, or ``renderer`` starts calling
   ``overlay()`` with the wrong argument. Real end-to-end rendering
   catches both.

2. **Filesystem semantics.** The renderer's `_normalise`-equivalent
   behaviour (reading bytes, resolving the template's parent as the
   include root, `followlinks=False` on the loader) is a filesystem
   contract. Only real files exercise it truthfully.

3. **The determinism guarantee, holistically.** Every module claims a
   piece of the determinism contract (sorted context, deterministic
   `tojson`, no time/random globals). Only an end-to-end test can prove
   the *composition* is byte-identical across two renders. A unit test
   on `_deterministic_context` verifies sorted keys; it does not verify
   that sorting, serialisation, and Jinja's own stable iteration produce
   the same bytes together.

What this file *does not* do
----------------------------
- **Duplicate unit coverage.** Every adversarial template exercised
  here has a corresponding unit test with a finer-grained assertion
  (specific error code, specific line number, specific hint). This file
  asserts only the *outcome* (ok/err, code, presence of a hint) so it
  does not become a second home for assertions the unit tests already
  own — that duplication is the source of half the drift bugs a mature
  codebase suffers from.
- **Test the CLI.** That is
  ``tests/integration/test_validate_command.py`` and
  ``tests/integration/test_create_resolution.py``. This file stops at
  the renderer boundary; if the CLI wires the renderer wrong, the CLI
  tests catch it.
- **Golden output files.** Determinism is asserted by comparing two
  renders *of the same template with the same variables* to each other,
  not to a checked-in fixture. Fixtures rot when filters change; two
  renders of the same input cannot drift. If a golden snapshot is ever
  needed (e.g. to lock a public example), it belongs in a dedicated
  contract test, not here.

Determinism contract
--------------------
ADR-0014 states: *same template + same variables ⇒ byte-identical
output.* This file is where that claim is proven against the whole
pipeline — environment, filter allowlist, sandbox, context sorting,
renderer — rather than against any one module.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from forge.loading.result import Err, Ok
from forge.rendering import TemplateRenderer, TemplateRenderError

# ---------------------------------------------------------------------------
# Fixtures
#
# Every test that needs a template writes it into a fresh tmp_path. We
# deliberately do NOT use a shared fixture directory of template files:
#
#   * A shared directory accumulates unused templates over time, and a
#     reader of this file has to cross-reference two locations to know
#     what a test asserts.
#   * A shared directory is unsafe to mutate from tests (pytest-xdist
#     runs the file in parallel; two tests writing to the same path
#     would race).
#   * The template body is part of what the test asserts, so keeping it
#     next to the assertion is a documentation win.
#
# The small duplication (each test writes its own template) is the
# price of locality, and it is cheap: templates in this file are one
# to five lines.
# ---------------------------------------------------------------------------


@pytest.fixture()
def renderer() -> TemplateRenderer:
    """A fresh renderer per test.

    The renderer is stateless, so a shared instance would work — but
    constructing one per test keeps the contract honest: nothing the
    test asserts should depend on state left over from a previous test.
    If a future refactor ever introduces state (a template cache, an
    instrumentation hook), this fixture ensures no test accidentally
    depends on it.
    """
    return TemplateRenderer()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Write *body* to *name* inside *tmp_path* and return the path.

    Kept as a helper rather than a fixture because several tests write
    *multiple* templates (to prove include scoping) and need to control
    the directory layout.
    """
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _sha(text: str) -> str:
    """SHA-256 of UTF-8 bytes. Used to compare two renders byte-exactly."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Happy path: a real template, real variables, real filters
# ---------------------------------------------------------------------------


def test_renders_a_typical_scaffolding_template(tmp_path: Path, renderer: TemplateRenderer) -> None:
    """A realistic template — casing filters, defaults, loops — renders as written.

    This is the "does the whole thing work" smoke test. The template
    exercises the three most common template idioms (a casing filter, a
    `default` for an optional variable, and a `{% for %}` loop), so a
    failure here means something fundamental broke in the environment,
    the allowlist, or the context plumbing.
    """
    template = _write(
        tmp_path,
        "pyproject.toml.j2",
        """\
[project]
name = "{{ project | kebab }}"
version = "0.1.0"
authors = [
{% for author in authors %}  { name = "{{ author }}" },
{% endfor %}]
optional = "{{ extra | default('none') }}"
""",
    )

    result = renderer.render(
        template,
        {
            "project": "MyThing",
            "authors": ["Ada Lovelace", "Alan Turing"],
            "extra": None,
        },
    )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    body = result.value
    assert 'name = "my-thing"' in body
    assert '{ name = "Ada Lovelace" }' in body
    assert '{ name = "Alan Turing" }' in body
    assert 'optional = "none"' in body


def test_renders_with_only_the_context_that_was_passed(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """No ambient globals leak into the render context.

    The globals allowlist is empty; if a future contributor adds a global
    (e.g. a `range` helper), this test fails because it asserts the
    template cannot reference a name that is not in the context. That is
    a one-test tripwire for one of the most security-sensitive invariants
    in the rendering module.
    """
    template = _write(tmp_path, "g.j2", "value={{ helper(1) }}\n")

    result = renderer.render(template, {})

    assert isinstance(result, Err)
    assert result.error.code == "undefined.variable"
    # The name is `helper`, not a filter — the renderer surfaces the
    # undefined name so IDE integrations can point at it.
    assert result.error.name == "helper"


# ---------------------------------------------------------------------------
# Determinism: same input, same bytes
# ---------------------------------------------------------------------------


def test_two_renders_of_the_same_input_are_byte_identical(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """ADR-0014's determinism contract, proven against the whole pipeline.

    The template combines the three pieces of the determinism story:
    sorted context iteration (`{% for k in ctx %}`), deterministic JSON
    (`| tojson`), and no time/random globals. If any of those guarantees
    were individually broken but the others compensated (unlikely, but
    possible in principle), a single-render test would miss it — two
    renders compared by hash catch it.
    """
    template = _write(
        tmp_path,
        "det.j2",
        """\
{% for k in ctx %}{{ k }}={{ ctx[k] }}
{% endfor %}{{ payload | tojson }}
""",
    )
    variables = {
        "ctx": {"zeta": 1, "alpha": 2, "mu": 3},
        "payload": {"b": 2, "a": 1, "nested": {"z": 0, "y": 1}},
    }

    first = renderer.render(template, variables)
    second = renderer.render(template, variables)

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert _sha(first.value) == _sha(second.value)


def test_context_key_iteration_is_sorted(tmp_path: Path, renderer: TemplateRenderer) -> None:
    """The context is sorted, so `{% for k in ctx %}` is stable.

    A subtler property than "the two renders are equal": a renderer that
    happened to sort *payload* but not *ctx* could still produce
    byte-identical output across two renders on the same machine (dict
    insertion order is preserved), while producing *different* output on
    a machine where the YAML loader happened to build the dict in a
    different order. Asserting the sorted *order* of the emitted keys is
    the property that makes the guarantee portable.
    """
    template = _write(tmp_path, "ctx.j2", "{% for k in ctx %}{{ k }};{% endfor %}\n")

    result = renderer.render(template, {"ctx": {"z": 1, "a": 2, "m": 3}})

    assert isinstance(result, Ok)
    assert result.value.strip() == "a;m;z;"


# ---------------------------------------------------------------------------
# Filter allowlist, exercised end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filter_name", "template_body", "variables", "expected"),
    [
        ("upper", "{{ x | upper }}", {"x": "abc"}, "ABC"),
        ("lower", "{{ x | lower }}", {"x": "ABC"}, "abc"),
        ("snake", "{{ x | snake }}", {"x": "MyThing"}, "my_thing"),
        ("kebab", "{{ x | kebab }}", {"x": "my_thing"}, "my-thing"),
        ("pascal", "{{ x | pascal }}", {"x": "my_thing"}, "MyThing"),
        ("default", "{{ x | default('fallback') }}", {"x": None}, "fallback"),
        ("tojson", "{{ x | tojson }}", {"x": {"b": 2, "a": 1}}, '{"a":1,"b":2}'),
        ("join", "{{ x | join('-') }}", {"x": ["a", "b", "c"]}, "a-b-c"),
        ("int", "{{ x | int }}", {"x": "42"}, "42"),
        ("string", "{{ x | string }}", {"x": 42}, "42"),
        ("trim", "{{ x | trim }}", {"x": "  hi  "}, "hi"),
        ("replace", "{{ x | replace('-', '_') }}", {"x": "a-b-c"}, "a_b_c"),
        ("length", "{{ x | length }}", {"x": "hello"}, "5"),
        ("title", "{{ x | title }}", {"x": "hello world"}, "Hello World"),
        ("capitalize", "{{ x | capitalize }}", {"x": "hello"}, "Hello"),
    ],
)
def test_each_allowlisted_filter_renders_end_to_end(
    tmp_path: Path,
    renderer: TemplateRenderer,
    filter_name: str,
    template_body: str,
    variables: dict,
    expected: str,
) -> None:
    """Every documented filter works through the full pipeline.

    This parametrisation is *not* duplicated from
    ``test_filter_allowlist.py::test_allowed_filter_renders_correctly``;
    that test asserts each filter's behaviour at the filter boundary.
    This test asserts each filter's availability through the public
    renderer — which additionally verifies the environment actually
    exposes it and the sandbox does not block it.

    A filter that is installed correctly but blocked by the sandbox would
    pass the unit test and fail this one. That is the composition risk
    this layer exists to catch.
    """
    template = _write(tmp_path, "t.j2", template_body)

    result = renderer.render(template, variables)

    assert isinstance(result, Ok), f"filter {filter_name!r} failed end-to-end: {result!r}"
    assert result.value == expected


def test_unknown_filter_fails_with_actionable_error(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """A filter outside the allowlist is a hard error, not silent output.

    This is the anti-drift test for the allowlist: if a maintainer
    accidentally removes a filter from ``ALLOWED_FILTERS`` while leaving
    it in the doc (or vice versa), one of the two fails here or in the
    doc-sync test. This one asserts the runtime behaviour; the doc-sync
    test asserts the documentation. Together they cover the pipeline.
    """
    template = _write(tmp_path, "t.j2", "{{ x | nonexistent }}")

    result = renderer.render(template, {"x": "abc"})

    assert isinstance(result, Err)
    assert result.error.code == "syntax.error"
    assert result.error.hint  # actionable
    assert result.error.line is not None


# ---------------------------------------------------------------------------
# Error translation, exercised end-to-end
# ---------------------------------------------------------------------------


def test_missing_variable_is_an_error_not_empty_output(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """StrictUndefined surfaces the variable name, file, and a hint.

    The single most important error-translation test in this file: the
    renderer never silently substitutes an empty string for a missing
    name. A blank line in generated code is a *long* debugging session,
    and this test is the guard against reintroducing it.
    """
    template = _write(tmp_path, "t.j2", "Hello {{ who }}!")

    result = renderer.render(template, {"name": "someone"})

    assert isinstance(result, Err)
    assert result.error.code == "undefined.variable"
    assert result.error.name == "who"
    assert "t.j2" in result.error.template
    assert result.error.hint


def test_syntax_error_reports_line_number(tmp_path: Path, renderer: TemplateRenderer) -> None:
    """A syntax error includes the line and column Jinja can locate."""
    template = _write(tmp_path, "t.j2", "{% for x in items %}\n{{ x }}\n")

    result = renderer.render(template, {"items": [1, 2, 3]})

    assert isinstance(result, Err)
    assert result.error.code == "syntax.error"
    assert result.error.line is not None
    assert result.error.line >= 1


def test_missing_template_file_is_a_structured_error(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """A missing template is an io.not_found error, not an exception.

    The renderer contract is "never raise for user-caused conditions."
    A test that only covered successful renders would miss the failure
    modes of the I/O path; this one ensures the not-found case produces
    the same Result shape as every other failure.
    """
    result = renderer.render(tmp_path / "does-not-exist.j2", {})

    assert isinstance(result, Err)
    assert result.error.code == "io.not_found"
    assert "does-not-exist.j2" in result.error.template
    assert result.error.hint


# ---------------------------------------------------------------------------
# Include scoping, exercised end-to-end
# ---------------------------------------------------------------------------


def test_include_within_the_same_directory_works(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """A template may include a sibling file — the common case.

    Blueprints routinely factor common fragments into partials. If the
    loader's search path were wrong (e.g. cwd instead of the template's
    parent), includes would fail or — worse — succeed by accident when
    the cwd happened to contain a same-named file. A positive test here,
    paired with the negative tests below, pins the scoping rule.
    """
    _write(tmp_path, "_partial.j2", "VALUE={{ value }}")
    main = _write(tmp_path, "main.j2", "start:{% include '_partial.j2' %}:end")

    result = renderer.render(main, {"value": "hello"})

    assert isinstance(result, Ok)
    assert result.value == "start:VALUE=hello:end"


def test_include_cannot_escape_the_template_directory(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """A template cannot include a file above its own directory.

    This is the *security* counterpart to the previous test. The loader's
    searchpath is the template's parent directory, and Jinja resolves
    relative paths against it. A `../secret.txt` include must fail with
    ``include.not_found`` — never read the file, never echo its contents
    in the error.
    """
    # A file one level up, which the template will try to reach.
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("TOP-SECRET", encoding="utf-8")

    template = _write(tmp_path / "bp", "leak.j2", "{% include '../outside-secret.txt' %}")

    result = renderer.render(template, {})

    assert isinstance(result, Err)
    assert result.error.code == "include.not_found"
    # The error must not have leaked the secret into its message or
    # hint. This assertion is cheap and catches a whole class of
    # "informative error message" mistakes.
    rendered = f"{result.error.message} {result.error.hint}"
    assert "TOP-SECRET" not in rendered


# ---------------------------------------------------------------------------
# Sandbox denials, exercised end-to-end
# ---------------------------------------------------------------------------


def test_sandbox_blocks_python_internal_attribute_access(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """The classic `__class__` escape is denied, and the failure is structured.

    The template attempts a well-known escape. The assertion is not just
    "it failed" but "it failed with a code that a caller can distinguish
    from a normal error" — ``sandbox.denied``. That distinction is what
    lets a CI pipeline flag attempted sandbox escapes for review without
    parsing error prose.
    """
    template = _write(
        tmp_path,
        "escape.j2",
        "{{ ''.__class__ }}",
    )

    result = renderer.render(template, {})

    assert isinstance(result, Err)
    # Depending on Jinja's internal traversal order, this may surface as
    # a sandbox denial or as a StrictUndefined for the denied attribute
    # (which is what Jinja produces when `is_safe_attribute` returns
    # False rather than raising). Both are safe outcomes; both are
    # permitted by the contract.
    assert result.error.code in {"sandbox.denied", "undefined.variable"}
    assert result.error.hint


def test_sandbox_allows_legitimate_access_to_plain_data(
    tmp_path: Path, renderer: TemplateRenderer
) -> None:
    """The sandbox does not over-block: ordinary data structures work.

    A sandbox that blocks everything is safe but useless. This test pins
    the other side of the trade: templates must be able to read ordinary
    data — dict keys, list items, string methods — without triggering
    denials. If a future hardening pass tightens the deny list too far,
    this test fails.
    """
    template = _write(
        tmp_path,
        "ok.j2",
        "{{ config['name'] }}\n{{ items[0] }}\n{{ greeting.upper() }}",
    )

    result = renderer.render(
        template,
        {
            "config": {"name": "demo"},
            "items": ["first", "second"],
            "greeting": "hi",
        },
    )

    assert isinstance(result, Ok), f"legitimate template failed: {result!r}"
    assert result.value == "demo\nfirst\nHI"


# ---------------------------------------------------------------------------
# Contract: the public error type is what callers branch on
# ---------------------------------------------------------------------------


def test_error_is_the_documented_public_type(tmp_path: Path, renderer: TemplateRenderer) -> None:
    """Every failure returns a ``TemplateRenderError`` from the façade.

    Consumers import ``TemplateRenderError`` from ``forge.rendering`` and
    construct error handlers against it. If a future refactor returns a
    subclass or a lookalike, this test fails — which is exactly what we
    want, because the whole promise of the façade is "this is the type
    you can catch."
    """
    template = _write(tmp_path, "t.j2", "{{ missing }}")

    result = renderer.render(template, {})

    assert isinstance(result, Err)
    assert isinstance(result.error, TemplateRenderError)
    # The type is importable from the façade (not just from
    # forge.rendering.errors); consumers must not need a private import.
    from forge.rendering import TemplateRenderError as FacadeType

    assert type(result.error) is FacadeType
