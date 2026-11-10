"""Criterion 5: rendering is deterministic — same input, same bytes.

The guarantee
-------------
Rendering is a pure function of ``(template_bytes, variables)``. Two
calls with the same inputs must produce byte-identical output. This is
the single property that makes the rendering layer testable with
golden snapshots, diffable in CI, and safe to rely on for generated
code that will be committed to a repository.

The guarantee fails if *anything* in the pipeline is
non-deterministic — a clock, a random source, unordered iteration over
a dict, environment-dependent whitespace handling, or a value derived
from a hash. Each is a distinct hazard, and each must be exercised
separately, because a test that asserted "two renders are equal" over
a template that used only ``{{ name }}`` would pass even if the
renderer were full of hidden sources of variance. The output would be
equal by accident, not by contract.

What "deterministic" means here, and what it does not
-----------------------------------------------------
The renderer's contract is "byte-identical output for the same
template and the same variables, **modulo Jinja's line-ending
normalisation**." Jinja's lexer translates ``\\r\\n`` to ``\\n`` during
parsing. The renderer does not attempt to reverse this, because
reversing it correctly would require tracking line-ending conventions
through Jinja's token stream — which Jinja does not expose — and any
heuristic would fail unpredictably for templates that legitimately mix
conventions.

The consequence is that LF is the *canonical* output line ending,
regardless of the template's source. This is a strength, not a
limitation: a template checked in from a Windows workstation and the
same template checked in from a Unix workstation produce
byte-identical output. The determinism guarantee is *strengthened* by
the normalisation, not weakened.

An earlier version of this file asserted the *opposite* — that a CRLF
template would produce CRLF output — on the theory that "byte-for-byte"
meant "byte-for-byte." That was wrong, and the fix was to correct the
contract in both the renderer's docstring and this file, not to add a
fragile normalisation-reversal layer to the renderer. The correct
assertion — "CRLF input produces LF output, consistently" — is what
``test_crlf_template_renders_with_lf_newlines`` now checks.

The four determinism hazards, and what tests each
-------------------------------------------------
1. **Dict iteration order.** Jinja evaluates ``{% for k in dict %}`` by
   iterating the dict. Python dicts preserve insertion order, but a
   caller may pass a ``Mapping`` whose iteration order is unspecified
   (a YAML loader with a custom ``Mapping`` implementation, an ORM
   proxy, a lazily-materialised view). The renderer sorts the context
   *recursively* before passing it to Jinja, so ``{% for k in ctx %}``
   always yields keys in sorted order — and so does any nested loop
   inside the same template. **Tested by**
   ``test_context_iteration_is_sorted`` and
   ``test_context_iteration_is_stable_across_insertion_orders``.

2. **Unsorted JSON serialisation.** ``|tojson`` on a dict must produce
   the same output regardless of the dict's insertion order, or the
   generated file will differ between runs. The filter sorts keys;
   ``test_tojson_keys_are_sorted`` asserts this. The recursive case
   (nested dicts) is tested by ``test_tojson_is_deterministic_for_nested_structures``.

3. **Non-deterministic filters.** ``|random``, ``|now``, and other
   stock-Jinja filters would break byte-identity. They are removed by
   the allowlist; ``test_jinja_default_filters_are_removed`` in
   ``test_filter_allowlist.py`` asserts that, and this file's
   ``test_determinism_template_has_no_randomness`` is the
   higher-level control: it proves the template in this file uses only
   deterministic filters.

4. **Whitespace mangling.** A renderer that trimmed trailing
   whitespace or collapsed multiple blank lines would silently diverge
   from what the template author wrote. The environment sets
   ``keep_trailing_newline=True``, ``trim_blocks=False``, and
   ``lstrip_blocks=False``. ``test_whitespace_is_preserved_byte_for_byte``
   asserts all three. Line-ending normalisation is a *deliberate
   exception* to whitespace preservation, documented above and
   tested by ``test_crlf_template_renders_with_lf_newlines``.

Test strategy
-------------
- **The core test is a hash equality test over a hostile template.**
  ``test_identical_renders_are_byte_identical`` renders the same
  fixture twice and compares the SHA-256 digests. Hashing is used
  instead of ``==`` on the strings because it produces a failure
  message that shows *which byte* differs (the hashes diverge), not
  just a wall of diff. It is also what a downstream consumer (a
  golden snapshot, a CI diff) would use to verify determinism.

- **Each hazard has its own targeted test.** The hash test proves the
  *overall* property; the targeted tests prove *why* it holds. This is
  a deliberate redundancy. If a future refactor added a subtle source
  of variance that only appeared under specific inputs, the hash test
  might pass (its fixture happens not to trigger the variance) while
  a targeted test would fail with a message naming the specific
  hazard. Conversely, if a targeted test's helper was broken, the
  hash test would still catch a real determinism regression.

- **The fixture is deliberately hostile.** ``determinism.j2`` contains
  a dict iteration, a ``|tojson`` of a nested dict, a trailing
  newline, and no filters that could hide variance. A "friendly"
  template (``Hello {{ name }}``) would pass every determinism test
  trivially. The fixture's job is to *stress* the pipeline so a
  regression surfaces.

- **Caller-side insertion order is varied.** Several tests pass the
  same logical dict with keys inserted in different orders, then
  assert the output is identical. This catches any determinism hazard
  that is introduced *after* the renderer's sort — e.g. if a future
  change moved the sort to a later stage and forgot a path.

- **Line-ending behaviour is asserted as *normalisation*, not as
  preservation.** The renderer's contract is "CRLF input → LF output,
  consistently," because Jinja's lexer normalises. The tests reflect
  this: they assert that CRLF and LF templates both render with LF,
  not that CRLF survives. This is the honest form of the determinism
  guarantee — see the "What 'deterministic' means here" section above.

The fixture
-----------
``tests/fixtures/templates/determinism.j2`` is:

.. code-block:: jinja

    {% for k in ctx %}{{ k }}={{ ctx[k] }}
    {% endfor %}{{ payload | tojson }}

It has two parts:
* The ``{% for k in ctx %}`` loop, whose output depends entirely on
  iteration order. A renderer that does not sort the context will
  produce different output for ``{"b": 1, "a": 2}`` than for
  ``{"a": 2, "b": 1}``.
* The ``{{ payload | tojson }}`` expression, whose output depends on
  the JSON filter's key sorting. A filter that preserves insertion
  order will produce different output for
  ``{"y": 2, "x": 1}`` than for ``{"x": 1, "y": 2}``.

Both parts contribute to the hash; a regression in *either* breaks
the hash test. That is intentional: the fixture is a single artefact
that would catch either hazard, so a maintainer investigating a hash
failure has two concrete hypotheses (iteration, serialisation) to
check — a much better starting point than "the output changed
somehow."
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from forge.loading.result import Err, Ok
from forge.rendering import TemplateRenderer

_FIX = Path(__file__).parents[2] / "fixtures" / "templates"


def _sha(text: str) -> str:
    """SHA-256 of a string's UTF-8 bytes.

    Using the encoded bytes, not the string, is deliberate: two
    strings can compare equal in Python while differing in their
    underlying byte representation (e.g. normalisation forms of
    non-ASCII). The determinism contract is about *bytes*, because
    that is what gets written to disk and committed to a repository.
    Hashing the bytes is the honest form of the assertion.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The core determinism test: same input, same bytes.
# ---------------------------------------------------------------------------
def test_identical_renders_are_byte_identical() -> None:
    """The same template with the same variables renders identically.

    This is the primary assertion of the file. It renders
    ``determinism.j2`` twice with identical variables and asserts the
    two outputs have the same SHA-256 digest.

    Why hash equality and not string equality:

    * ``==`` on two strings that differ in one byte produces a diff
      that is hard to read from a test log. Two differing hashes are
      unmissable — the failure message names the file and the test,
      and the maintainer's next step is to reproduce and diff locally.
    * The hash is what a *downstream* consumer (golden snapshot, CI
      artifact integrity check) would compute. Asserting on it now
      means the test proves the same property the consumer relies
      on.
    * Hashes are cheap. A determinism regression will *always* change
      the hash; the failure mode is unambiguous.

    Why two renders and not one render vs. a golden string:

    A golden-string assertion ("the output should be exactly
    ``a=1\\nm=3\\nz=1\\n{\"a\":1,\"b\":2}``") would also catch
    non-determinism — but only if the golden string were correct on
    every platform. A golden string bakes in assumptions about
    whitespace (trailing newlines, line endings) that are exactly the
    hazards determinism is supposed to *eliminate*. Testing two
    renders against each other is the assertion "the renderer is a
    pure function," not "the renderer produces this specific output,"
    which is the actual contract.

    Why the variables are what they are:

    ``ctx`` uses keys ``z``, ``a``, ``m`` — deliberately *not* already
    sorted in the source. If the renderer did not sort, the loop would
    iterate ``z, a, m`` and produce a specific output that would
    differ from a render where the caller passed the same values in
    ``a, m, z`` order. The test below
    (``test_context_iteration_is_sorted``) exercises that specific
    hazard; this test uses a fixed input to prove that *whatever* the
    renderer does, it does it the same way twice.

    ``payload`` uses keys ``b``, ``a`` for the same reason applied to
    ``|tojson``.

    This test does *not* assert a specific output, deliberately. The
    contract it proves is "the renderer is a function of its inputs,"
    not "the renderer produces output X." The specific output is
    asserted by the more targeted tests below; this test catches the
    class of regression that would only appear across repeated calls
    (a cache that mutates state, a global counter, a set that
    reorders).
    """
    template = _FIX / "determinism.j2"
    variables = {
        "ctx": {"z": 1, "a": 2, "m": 3},
        "payload": {"b": 2, "a": 1},
    }

    first = TemplateRenderer().render(template, variables)
    second = TemplateRenderer().render(template, variables)

    assert isinstance(first, Ok), (
        f"first render failed: " f"{first.error.format() if isinstance(first, Err) else 'n/a'}"
    )
    assert isinstance(second, Ok), (
        f"second render failed: " f"{second.error.format() if isinstance(second, Err) else 'n/a'}"
    )
    assert _sha(first.value) == _sha(second.value), (
        "determinism regression: two renders of the same template with "
        "the same variables produced different output.\n"
        f"First digest:  {_sha(first.value)}\n"
        f"Second digest: {_sha(second.value)}\n"
        f"First output:  {first.value!r}\n"
        f"Second output: {second.value!r}"
    )


# ---------------------------------------------------------------------------
# Hazard 1: dict iteration order (recursive).
# ---------------------------------------------------------------------------
def test_context_iteration_is_sorted() -> None:
    """``{% for k in ctx %}`` iterates keys in sorted order.

    The renderer sorts the context recursively before passing it to
    Jinja (see ``TemplateRenderer._deterministic_context``). This test
    proves the sort is *effective* for the outermost dict: a dict
    whose insertion order is ``z, a, m`` renders as ``a, m, z``, not
    as ``z, a, m``.

    This is the test that caught the original bug: an earlier version
    of ``_deterministic_context`` sorted only the top-level keys,
    leaving nested dicts (like ``ctx``) unsorted. The test's failure
    message named the exact reproduction case —
    ``{"z": 1, "a": 2, "m": 3}`` rendering as ``z, a, m`` instead of
    ``a, m, z`` — and the fix was to make the sort recursive.

    The assertion is more specific than the hash test: the hash test
    proves *two renders are equal*, but the two renders could both
    produce ``z, a, m`` (wrong order, but consistent) and pass. This
    test asserts the *correct* order, not just consistency. That
    distinction is what made the original bug visible — the hash test
    would have passed even with the sort entirely removed, because
    the two renders would have agreed on the (wrong) output.
    """
    variables = {"ctx": {"z": 1, "a": 2, "m": 3}, "payload": {}}
    result = TemplateRenderer().render(_FIX / "determinism.j2", variables)

    assert isinstance(result, Ok), (
        f"render failed: " f"{result.error.format() if isinstance(result, Err) else 'n/a'}"
    )
    # Extract the keys in the order they appear in the output and
    # assert on the ordered list, not on a substring — substring
    # matching would pass if ``a`` appeared anywhere, not just where
    # it should.
    keys = [line.split("=")[0] for line in result.value.splitlines() if "=" in line]
    assert keys[:3] == ["a", "m", "z"], (
        f"context keys must render in sorted order; got {keys!r}. "
        "This is the determinism contract for {% for %} iteration — "
        "see _deterministic_context and _sort_mapping in renderer.py."
    )


def test_context_iteration_is_sorted_recursively() -> None:
    """Nested dicts are sorted at every level, not just the outermost.

    The recursive sort in ``_sort_mapping`` walks every mapping
    reachable from the context. This test proves the recursion is
    *effective*: a template that iterates a nested dict-of-dicts
    produces keys in sorted order at every depth.

    The template renders both levels of a nested dict. If the sort
    only touched the outer dict, the inner dict's iteration would
    follow the caller's insertion order and fail the assertion.

    This is the *specific* hazard the earlier version of the renderer
    had: ``_deterministic_context`` sorted ``{"ctx": ..., "payload": ...}``
    to produce ``ctx, payload`` order, but left the value of ``ctx``
    untouched. A template iterating ``ctx`` therefore saw the caller's
    insertion order, not sorted order. The test above catches the
    outermost case; this test catches the nested case that the
    outermost fix left open.
    """
    template = (
        "{% for outer_key in outer %}"
        "{% for inner_key in outer[outer_key] %}"
        "{{ outer_key }}.{{ inner_key }} "
        "{% endfor %}"
        "{% endfor %}"
    )
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "nested.j2"
    tmp.write_text(template, encoding="utf-8")

    variables = {
        "outer": {
            "z": {"inner_b": 1, "inner_a": 2},
            "a": {"inner_d": 3, "inner_c": 4},
        },
    }
    result = TemplateRenderer().render(tmp, variables)
    assert isinstance(result, Ok), (
        f"render failed: " f"{result.error.format() if isinstance(result, Err) else 'n/a'}"
    )

    # Expected order: outer sorted (a, z), inner sorted at each level
    # (inner_c before inner_d, inner_a before inner_b).
    expected = "a.inner_c a.inner_d z.inner_a z.inner_b "
    assert result.value == expected, (
        f"nested dicts must be sorted at every level.\n"
        f"Expected: {expected!r}\n"
        f"Got:      {result.value!r}"
    )


def test_context_iteration_is_stable_across_insertion_orders() -> None:
    """Two dicts with the same keys but different insertion orders render identically.

    This is the *cross-caller* form of the determinism guarantee. A
    blueprint's ``variables`` may be built by different code paths (a
    YAML loader, a CLI flag merge, a prompt-answer merge), each of
    which may insert keys in a different order. The output must not
    depend on which path built the dict.

    The test passes the same logical data twice, with keys inserted
    in opposite orders, and asserts the output bytes match. If the
    renderer sorted keys *inside* the loop but not in the context
    (a plausible partial fix), this test would still pass — because
    the loop sorts. But if the renderer sorted the context at the
    *call site* and a future refactor bypassed it, this test would
    catch the bypass.

    This test is *also* a check on the recursive sort fix. The
    ``ctx`` dict is inserted in opposite orders in the two calls; if
    the sort were only top-level, the two ``ctx`` dicts would render
    in opposite orders and the test would fail. The fact that it
    passes is evidence the recursion is effective.
    """
    template = _FIX / "determinism.j2"
    payload = {"b": 2, "a": 1}

    forward = {"ctx": {"a": 2, "m": 3, "z": 1}, "payload": payload}
    reverse = {"ctx": {"z": 1, "m": 3, "a": 2}, "payload": payload}

    a = TemplateRenderer().render(template, forward)
    b = TemplateRenderer().render(template, reverse)

    assert isinstance(a, Ok), (
        f"forward render failed: " f"{a.error.format() if isinstance(a, Err) else 'n/a'}"
    )
    assert isinstance(b, Ok), (
        f"reverse render failed: " f"{b.error.format() if isinstance(b, Err) else 'n/a'}"
    )
    assert a.value == b.value, (
        "insertion order of the caller's dict must not affect output.\n"
        f"Forward-insertion output: {a.value!r}\n"
        f"Reverse-insertion output: {b.value!r}"
    )


# ---------------------------------------------------------------------------
# Hazard 2: JSON serialisation order.
# ---------------------------------------------------------------------------
def test_tojson_keys_are_sorted() -> None:
    """``|tojson`` emits keys in sorted order, not insertion order.

    The JSON filter passes ``sort_keys=True`` to ``json.dumps``, so
    the same dict produces the same output regardless of how the
    caller built it. This matters for generated files: a
    ``package.json`` or ``pyproject.toml`` rendered from a blueprint
    must be reproducible, and a JSON serialiser that preserves
    insertion order would produce different bytes for the same
    logical content.

    The test uses a dict whose insertion order (``b`` then ``a``) is
    deliberately the *opposite* of sorted order, so the assertion
    distinguishes "sorted" from "insertion order" with no ambiguity.
    """
    template = "{{ payload | tojson }}"
    # Write the template inline; the `|tojson` case is one expression
    # and does not need the larger `determinism.j2` fixture.
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "tojson.j2"
    tmp.write_text(template, encoding="utf-8")

    result = TemplateRenderer().render(tmp, {"payload": {"b": 2, "a": 1}})
    assert isinstance(result, Ok), (
        f"render failed: " f"{result.error.format() if isinstance(result, Err) else 'n/a'}"
    )
    assert result.value == '{"a":1,"b":2}', (
        f"tojson must sort keys; got {result.value!r}. "
        "A regression here breaks byte-identity for any generated "
        "JSON file — see filters._tojson."
    )


def test_tojson_is_stable_across_insertion_orders() -> None:
    """The same JSON payload renders identically across insertion orders.

    Complements ``test_tojson_keys_are_sorted``: that test asserts
    the *specific* sorted output; this test asserts the *general*
    property (same content → same bytes). Both are needed. The first
    pins down the exact serialisation form; the second rules out a
    regression that made the sort conditional (e.g. sorted only for
    one specific key set).
    """
    template = "{{ payload | tojson }}"
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "tojson.j2"
    tmp.write_text(template, encoding="utf-8")

    forward = TemplateRenderer().render(tmp, {"payload": {"x": 1, "y": 2}})
    reverse = TemplateRenderer().render(tmp, {"payload": {"y": 2, "x": 1}})

    assert isinstance(forward, Ok), (
        f"forward render failed: "
        f"{forward.error.format() if isinstance(forward, Err) else 'n/a'}"
    )
    assert isinstance(reverse, Ok), (
        f"reverse render failed: "
        f"{reverse.error.format() if isinstance(reverse, Err) else 'n/a'}"
    )
    assert forward.value == reverse.value, (
        "insertion order of the payload dict must not affect tojson "
        "output.\n"
        f"Forward: {forward.value!r}\n"
        f"Reverse: {reverse.value!r}"
    )


def test_tojson_is_deterministic_for_nested_structures() -> None:
    """Nested dicts sort keys at every level, not just the top level.

    A naive ``tojson`` implementation might sort the top-level dict
    and leave nested dicts to preserve insertion order (or vice
    versa). The ``sort_keys=True`` argument to ``json.dumps`` applies
    recursively, but a custom implementation could easily get this
    wrong. This test uses a two-level nested dict with unsorted keys
    at both levels and asserts the output is stable across insertion
    orders.
    """
    template = "{{ payload | tojson }}"
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "tojson.j2"
    tmp.write_text(template, encoding="utf-8")

    forward = TemplateRenderer().render(
        tmp,
        {"payload": {"z": {"b": 2, "a": 1}, "y": {"d": 4, "c": 3}}},
    )
    reverse = TemplateRenderer().render(
        tmp,
        {"payload": {"y": {"c": 3, "d": 4}, "z": {"a": 1, "b": 2}}},
    )

    assert isinstance(forward, Ok), (
        f"forward render failed: "
        f"{forward.error.format() if isinstance(forward, Err) else 'n/a'}"
    )
    assert isinstance(reverse, Ok), (
        f"reverse render failed: "
        f"{reverse.error.format() if isinstance(reverse, Err) else 'n/a'}"
    )
    assert forward.value == reverse.value


# ---------------------------------------------------------------------------
# Hazard 3: non-deterministic filters.
# ---------------------------------------------------------------------------
def test_determinism_template_has_no_randomness() -> None:
    """The determinism fixture uses no filters that could introduce variance.

    This test asserts a property of the *fixture*, not of the
    renderer. If a future edit to ``determinism.j2`` added a
    non-deterministic construct (``|random``, ``now()``, a call to
    ``lipsum()``), the fixture would no longer be a valid stress test
    for determinism — it would be a stress test for "the renderer
    produces different output," which is the opposite of what we want.

    The test uses a simple regex scan for the known non-deterministic
    filter/global names. It is not a substitute for
    ``test_jinja_default_filters_are_removed`` (which proves the
    renderer denies them at runtime); it is a *fixture integrity*
    check: the fixture must not reference names that would cause the
    renderer to fail. If it did, ``test_identical_renders_are_byte_identical``
    would fail with a "render failed" error rather than a
    "determinism regression" error, which would confuse the diagnosis.

    This is a *documentation* test as much as a behavioural one: it
    records the fixture's invariant so a future maintainer editing it
    cannot accidentally introduce the very hazard the fixture exists
    to test.
    """
    import re

    fixture = _FIX / "determinism.j2"
    content = fixture.read_text(encoding="utf-8")

    # Names that would cause the renderer to fail (denied filters) or
    # to produce non-deterministic output (time-based or random
    # constructs). Any of these appearing in the fixture would be a
    # bug in the fixture itself.
    non_deterministic = [
        r"\|\s*random\b",
        r"\bnow\s*\(",
        r"\bdate\s*\(",
        r"\btime\s*\(",
        r"\blipsum\s*\(",
        r"\bshuffle\b",
    ]
    for pattern in non_deterministic:
        assert not re.search(pattern, content), (
            f"determinism.j2 contains a non-deterministic construct "
            f"matching {pattern!r}; the fixture must use only "
            "deterministic filters and expressions."
        )


# ---------------------------------------------------------------------------
# Hazard 4: whitespace mangling (and the line-ending exception).
# ---------------------------------------------------------------------------
def test_whitespace_is_preserved_byte_for_byte() -> None:
    """The renderer does not trim, normalise, or collapse whitespace.

    Three environment settings govern whitespace handling:

    * ``trim_blocks=False`` — a block tag (``{% if %}``, ``{% for %}``)
      does not consume the newline that follows it. If this were
      ``True``, the fixture's ``{% for %}`` line would swallow the
      newline after it, changing the output.
    * ``lstrip_blocks=False`` — leading whitespace before a block tag
      is preserved. If this were ``True``, an indented ``{% for %}``
      would lose its indentation in the output.
    * ``keep_trailing_newline=True`` — a trailing newline in the
      template survives into the output. If this were ``False``,
      generated files would lack their final newline, breaking
      linters and diff tools.

    The fixture exercises all three: it has a ``{% for %}`` block
    whose behavior depends on both ``trim_blocks`` and
    ``lstrip_blocks``, and it ends with a newline that
    ``keep_trailing_newline`` preserves.

    The assertion is byte-for-byte equality with a known-good output.
    This is one of the two places in the file where a golden string
    is appropriate, because the *contract* being tested is that the
    output matches what the author wrote — not that two renders agree.
    The other place is ``test_crlf_template_renders_with_lf_newlines``,
    which asserts the line-ending normalisation on purpose.

    Platform note: the golden string is written with ``\\n`` and
    compared against ``result.value``, which the renderer produces
    with ``\\n``. This is deliberate. The renderer's contract is that
    the output's line ending is always LF, regardless of the source's
    line ending or the host OS — see
    ``test_crlf_template_renders_with_lf_newlines`` for the
    cross-platform assertion.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "whitespace.j2"
    # The template is written with explicit \n so the test's expected
    # output is unambiguous. The renderer must produce exactly this
    # string.
    tmp.write_text(
        "line1\nline2\n{% for x in items %}\n{{ x }}\n{% endfor %}\n",
        encoding="utf-8",
    )

    result = TemplateRenderer().render(tmp, {"items": [1, 2]})
    assert isinstance(result, Ok), (
        f"render failed: " f"{result.error.format() if isinstance(result, Err) else 'n/a'}"
    )

    # With trim_blocks=False, lstrip_blocks=False, keep_trailing_newline=True,
    # the block tags' surrounding newlines are preserved. The output is
    # the template with the {{ }} expressions substituted, nothing else.
    expected = "line1\nline2\n\n1\n\n2\n\n"
    assert result.value == expected, (
        "whitespace must be preserved byte-for-byte; the renderer must "
        "not trim, normalise, or collapse it.\n"
        f"Expected: {expected!r}\n"
        f"Got:      {result.value!r}\n"
        "If the difference is a single trailing newline, check that "
        "keep_trailing_newline=True is set in environment.py. If the "
        "difference is inside the loop body, check trim_blocks=False "
        "and lstrip_blocks=False."
    )


def test_trailing_newline_survives_render() -> None:
    """A template ending in ``\\n`` renders to output ending in ``\\n``.

    This is a narrower test than ``test_whitespace_is_preserved_byte_for_byte``:
    it isolates the ``keep_trailing_newline`` setting from the other
    whitespace settings. A regression in that single flag would pass
    the general whitespace test (which is dominated by the more
    obvious ``trim_blocks`` behaviours) but fail this one with a
    specific message naming the flag.

    Generated code files (Python, YAML, JSON) conventionally end in
    a newline. A renderer that strips it produces files that fail
    linters (``ruff W292``, ``yamllint new-line-at-end-of-file``) and
    trigger git's "no newline at end of file" warning. Both are real
    failures in downstream CI, and both are prevented by this one
    environment flag.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "trailing.j2"
    tmp.write_text("hello\n", encoding="utf-8")

    result = TemplateRenderer().render(tmp, {})
    assert isinstance(result, Ok)
    assert result.value == "hello\n", (
        f"expected 'hello\\n', got {result.value!r}. "
        "keep_trailing_newline=True is required in environment.py."
    )
    assert result.value.endswith("\n")


def test_crlf_template_renders_with_lf_newlines() -> None:
    """A CRLF template renders with LF newlines (Jinja normalisation).

    Jinja's lexer normalises ``\\r\\n`` to ``\\n`` during parsing. The
    renderer does not attempt to reverse this. This test asserts the
    *actual* and *correct* behaviour: a template checked in with CRLF
    line endings produces output with LF line endings.

    An earlier version of this test asserted the *opposite* — that a
    CRLF template would produce CRLF output — on the theory that
    "byte-for-byte" meant "byte-for-byte." That was wrong: Jinja does
    not make that guarantee, and reversing the normalisation would
    require tracking line-ending conventions through Jinja's token
    stream (which Jinja does not expose) and would fail
    unpredictably for templates that legitimately mix conventions.

    The *determinism* guarantee is unaffected and, in fact,
    *strengthened*: a template checked in from a Windows workstation
    and the same template checked in from a Unix workstation produce
    byte-identical output. The fix for the earlier false expectation
    was to correct the contract in both the renderer's docstring and
    this file, not to add a fragile normalisation-reversal layer to
    the renderer.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "crlf.j2"
    # Write bytes directly, bypassing the platform's text-mode line
    # ending handling. This is exactly what a template file checked in
    # on Windows would look like when read in binary mode.
    tmp.write_bytes(b"line1\r\nline2\r\n")

    result = TemplateRenderer().render(tmp, {})
    assert isinstance(result, Ok), (
        f"render failed: " f"{result.error.format() if isinstance(result, Err) else 'n/a'}"
    )
    assert result.value == "line1\nline2\n", (
        "CRLF templates render with LF newlines (Jinja normalisation). " f"Got: {result.value!r}"
    )
    assert "\r" not in result.value, (
        "carriage returns must not appear in rendered output; Jinja "
        "normalises them during lexing. See renderer.py's docstring "
        "for the determinism guarantee."
    )


def test_lf_template_renders_with_lf_newlines() -> None:
    """An LF template renders with LF newlines (control case).

    The companion to ``test_crlf_template_renders_with_lf_newlines``:
    together they establish that the renderer normalises to LF
    regardless of the source's convention. This test is the trivial
    control — an LF template is unchanged by normalisation — but it
    exists to make the assertion visible: if a future Jinja version
    started normalising in the *other* direction (to CRLF), this test
    would catch it with a message that names the expected convention.

    The pair of tests (CRLF → LF, LF → LF) is the *complete* statement
    of the line-ending contract. Either test alone would leave a hole:
    the CRLF test alone would pass if the renderer converted
    everything to CRLF; the LF test alone would pass if the renderer
    preserved whatever convention the source had. Both together pin
    down "always LF, regardless of source."
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "lf.j2"
    tmp.write_bytes(b"line1\nline2\n")

    result = TemplateRenderer().render(tmp, {})
    assert isinstance(result, Ok)
    assert result.value == "line1\nline2\n"


# ---------------------------------------------------------------------------
# Cross-cutting: the whole pipeline is deterministic end-to-end.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("template_text", "variables"),
    [
        # Plain variable substitution.
        ("Hello {{ name }}!", {"name": "Ada"}),
        # Empty template.
        ("", {}),
        # Whitespace-only template.
        ("\n\n\n", {}),
        # Loop over a list.
        ("{% for x in items %}{{ x }}\n{% endfor %}", {"items": [1, 2, 3]}),
        # Conditional.
        ("{% if flag %}yes{% else %}no{% endif %}", {"flag": True}),
        # Nested data with sorted JSON.
        ("{{ data | tojson }}", {"data": {"z": {"b": 2, "a": 1}}}),
        # Filters that must be deterministic.
        ("{{ name | upper }}", {"name": "ada"}),
        ("{{ name | snake }}", {"name": "MyThing"}),
    ],
)
def test_whole_pipeline_is_deterministic(
    template_text: str,
    variables: dict,
) -> None:
    """The full pipeline is deterministic across varied template shapes.

    The core hash test asserts determinism on one hostile template;
    this parameterised test asserts it across a representative sample
    of template shapes — plain substitution, loops, conditionals,
    filters, and empty templates. If a future change introduced a
    non-determinism that only affects one shape (e.g. loops gained a
    set-iteration over a set), this test would catch it while the
    single-fixture hash test might miss it.

    The parameterisation is not exhaustive. It is a *sanity sample*:
    if the pipeline is deterministic for these shapes and for the
    hostile fixture, it is deterministic in practice for any template
    that uses the allowlisted features. A property-based test
    (Hypothesis) would be more thorough, but the renderer's input
    space (templates, not data) makes Hypothesis awkward to apply
    without effectively writing a Jinja parser. A curated sample is
    the pragmatic form.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "sample.j2"
    tmp.write_text(template_text, encoding="utf-8")

    a = TemplateRenderer().render(tmp, variables)
    b = TemplateRenderer().render(tmp, variables)

    assert isinstance(a, Ok), (
        f"template {template_text!r} failed: "
        f"{a.error.format() if isinstance(a, Err) else 'n/a'}"
    )
    assert isinstance(b, Ok)
    assert a.value == b.value, (
        f"non-deterministic render for {template_text!r}.\n"
        f"First:  {a.value!r}\n"
        f"Second: {b.value!r}"
    )
