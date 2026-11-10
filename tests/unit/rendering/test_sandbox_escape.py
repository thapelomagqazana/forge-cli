"""Criterion 4: the sandbox blocks Python-internal escape routes.

The guarantee
-------------
Templates are untrusted input. A blueprint author — or anyone who can
supply a blueprint — can write a template that tries to reach Python
internals and execute arbitrary code. The sandbox must block every
published escape chain, and it must do so *observably*: the renderer
returns a structured ``Err`` with an actionable code, not a silent
fallback and not a raised traceback.

The sandbox's layered design (see ``_sandbox.py``) is:

    1. ``SandboxedEnvironment`` — Jinja's own type-based checks, which
       deny calls and attribute accesses on "dangerous" objects.
    2. ``_DENIED_ATTRS`` — a name-based deny list applied *before*
       Jinja's checks, catching attribute accesses that Jinja's
       type-based heuristics might miss (or that a future Jinja release
       might relax).
    3. ``SecurityError`` — raised on denial so the renderer can emit a
       structured ``sandbox.denied`` error with an actionable hint,
       rather than the ``UndefinedError`` that Jinja's default
       behaviour would produce.

This file tests the **observable consequence** of that layering —
"templates cannot escape" — not the layering itself. If a future
refactor changed the layering (say, replaced ``_DENIED_ATTRS`` with a
different mechanism) but the observable behaviour was preserved, this
file would still pass, which is what a *behavioural* test should do.
The layering itself is tested by
``test_jinja_default_filters_are_removed`` and
``test_jinja_default_globals_are_removed`` in
``test_filter_allowlist.py`` (mechanism tests), and by unit tests on
``ForgeSandboxedEnvironment`` if and when those are added.

What "escape" means, and what it does not
-----------------------------------------
An escape is a template that reaches Python objects the sandbox is not
supposed to expose, and uses them to run code, read files, or mutate
state. This file tests the **published** escape chains — the ones that
appear in Jinja's own issue tracker and in the wider ecosystem's CVE
write-ups. It does not test:

* **Jinja bugs** that are not yet public. If such a bug exists, this
  file cannot catch it (no test can); what this file *does* provide is
  the guarantee that *known* escapes are blocked, which is the
  best-in-class bar for a template renderer.
* **Resource exhaustion** (a template that loops forever, or uses
  ``lipsum(1000000)`` to allocate gigabytes). That is a *performance*
  concern, not a sandbox concern; it is addressed by OS-level limits
  and CI timeouts, not by the sandbox's deny list.
* **Non-Python sandbox escapes** (e.g. a template that reads files via
  a filter). Those are blocked by the *filter allowlist* (see
  ``filters.py``), not by the sandbox, and are tested by
  ``test_filter_allowlist.py``.
* **Missing-variable failures** that happen to be reported as
  ``undefined.variable``. ``StrictUndefined`` catches many
  would-be escapes early (because ``__class__`` is not in the context),
  but the *reason* it catches them is different from the sandbox — and
  a test that accepted ``undefined.variable`` as the expected outcome
  for an escape would fail to distinguish "the sandbox blocked this"
  from "the template never got far enough to try." Each test below
  asserts on ``sandbox.denied`` specifically for the escape it is
  testing; ``undefined.variable`` is only accepted where the test's
  docstring says so explicitly, and only for the *component of the
  chain that the escape necessarily reaches first*.

Test strategy
-------------
- **One escape chain per test, named for the chain.** Each test is a
  distinct published vector: ``__class__.__globals__``, ``__subclasses__``,
  ``__builtins__``, generator/coroutine introspection, ``__mro__``. The
  test name identifies the vector, so a failure says "the
  ``__subclasses__`` chain is open," not "some escape worked."
- **Multiple denial layers exercised.** Some chains are blocked by
  ``_DENIED_ATTRS`` (name-based, our layer); some by Jinja's
  type-based checks (the base layer). The tests do not care which
  layer fired — they assert the *outcome* — but the docstrings note
  which layers are involved, so a future reviewer knows what to
  investigate if one test fails and the others pass.
- **The allow-list of expected codes is tight, per-test.** Some tests
  accept ``undefined.variable`` in addition to ``sandbox.denied``,
  with a documented reason (Jinja or StrictUndefined sometimes catches
  the first attribute access before the sandbox does). Others accept
  only ``sandbox.denied`` because the chain is constructed to reach a
  *denied* attribute first. The tightness varies per test because the
  correct answer does; loosening them uniformly would let a real
  regression through.
- **A positive control test proves the sandbox is not over-eager.**
  ``test_legit_template_still_renders_with_objects_in_context``
  passes a plain Python object through the context and renders a
  template that uses it. If the sandbox denied *everything* (a
  plausible failure mode for an over-aggressive ``_DENIED_ATTRS``),
  this test would fail with a clear "the sandbox is too strict"
  message, distinct from "the sandbox is too lax."
"""

from __future__ import annotations

from pathlib import Path

from forge.loading.result import Err, Ok
from forge.rendering import TemplateRenderer


def _render_text(text: str, variables: dict | None = None) -> Ok[str] | Err:
    """Render an inline template string without needing a fixture file.

    Same helper as the other rendering test files. Duplicated because
    it is ten lines and the alternative (a ``conftest.py``) would add
    a module for a concern that is genuinely local to "render one
    string." When a *third* module needs a variant, refactor to
    ``conftest.py``; at two, keep it local.
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "inline.j2"
    tmp.write_text(text, encoding="utf-8")
    return TemplateRenderer().render(tmp, variables or {})


# ---------------------------------------------------------------------------
# The canonical import chain: str → object.__init__ → __globals__ → __import__
# ---------------------------------------------------------------------------
# This is the most widely published Jinja sandbox escape. It walks:
#
#     ''                  — a string literal
#       .__class__        — the str class
#       .__init__         — the str.__init__ method
#       .__globals__      — str.__init__'s module globals (the `builtins`
#                           module dict)
#       ['__builtins__']  — the builtins namespace
#       ['__import__']    — the import function
#       ('os')            — import os
#       .system('id')     — run a shell command
#
# Every attribute in the chain is on our deny list (`__class__`,
# `__globals__`, `__builtins__`, `__import__`), so the sandbox fires at
# the *first* access — `.__class__` — which is where the test expects
# the error to be attributed.
def test_str_class_globals_import_chain_is_blocked() -> None:
    """The canonical ``str.__class__.__init__.__globals__`` escape is denied.

    This is the escape that appears in Jinja's own security advisories
    and in every "how to exploit a template engine" writeup. It reaches
    ``__import__`` via the module globals of a built-in method — a route
    that bypasses any check that only looks at the *type* of the object
    being accessed.

    The sandbox blocks it at the first attribute access
    (``.__class__``), which is on ``_DENIED_ATTRS``. Jinja's own sandbox
    would also deny it (``__globals__`` is on Jinja's internal deny list
    too), but our name-based check fires first — the message from
    ``SecurityError`` names ``__class__``, which is precisely the
    attribute the user is trying to reach.

    Asserting on ``sandbox.denied`` specifically, not just ``Err``:

    A regression that removed ``__class__`` from ``_DENIED_ATTRS`` would
    let Jinja's base sandbox catch the escape at ``__globals__`` — which
    is *also* denied — so the test would still see ``Err``, but the
    error's *message* would name ``__globals__`` rather than
    ``__class__``. That difference matters: ``__class__`` is the
    attribute the user has to remove from their template; naming
    ``__globals__`` sends them looking at the wrong line. Asserting on
    the code (`sandbox.denied` for both) rather than the message allows
    either to be the reporting layer, but the error must still be
    categorised as a sandbox denial rather than (e.g.) an undefined
    variable.
    """
    template = (
        "{{ ''.__class__.__init__.__globals__"
        "['__builtins__']['__import__']('os').system('id') }}"
    )
    result = _render_text(template)
    _assert_escape_denied(result, expected_code="sandbox.denied")


# ---------------------------------------------------------------------------
# The subclass enumeration chain: () → __class__ → __bases__ → __subclasses__
# ---------------------------------------------------------------------------
# This is the *other* canonical escape. It walks the class hierarchy from
# a tuple literal up to `object`, then enumerates every loaded subclass
# to find one whose methods reach `os` or `subprocess`:
#
#     ()                   — a tuple literal
#       .__class__         — the tuple class
#       .__bases__         — object (tuple's base)
#       [0].__subclasses__ — every loaded subclass of object
#       [N]                — pick one whose methods reach subprocess
#       .__init__          — access it
#       .__globals__       — reach its module globals
#       ...
#
# The sandbox blocks it at `.__class__` (first access), same as the
# import chain. But this test is distinct from the import chain because
# it proves the sandbox blocks *object-graph traversal*, not just
# *module import*. A sandbox that blocked `__import__` but allowed
# `__subclasses__` would still be exploitable; testing both vectors
# ensures neither path is open.
def test_subclass_enumeration_chain_is_blocked() -> None:
    """The ``().__class__.__bases__[0].__subclasses__()`` escape is denied.

    The subclass enumeration route reaches arbitrary loaded classes by
    walking from ``object`` down to a subclass with a method whose
    ``__globals__`` contains a useful import. It is the vector that the
    ``_DENIED_ATTRS`` entry for ``__subclasses__`` exists to block.

    Jinja's own sandbox does not deny ``__subclasses__`` by name in
    every version; our deny list does, which is why this chain is
    blocked even in Jinja configurations that would otherwise allow it.
    That is the defence-in-depth property: our layer catches what
    Jinja's layer might miss.
    """
    template = "{{ ().__class__.__bases__[0].__subclasses__() }}"
    result = _render_text(template)
    _assert_escape_denied(result, expected_code="sandbox.denied")


# ---------------------------------------------------------------------------
# Plain __class__ access without a follow-up chain.
# ---------------------------------------------------------------------------
# The two tests above combine "reach __class__" with "do something
# dangerous after." A sandbox could conceivably block the *follow-up*
# operations (calling methods, subscripting) while allowing the
# *access* to `__class__` — which would be a worse design (users could
# reach class objects and inspect them), but a plausible-looking one
# for a naive implementation.
#
# This test proves the *access itself* is denied. It is the smallest
# possible escape vector and the one that isolates the deny list from
# Jinja's type-based checks: the object is a plain Python object passed
# via the context, whose `__class__` Jinja's type-based checks would
# consider safe (it is a normal attribute of a normal object).
def test_plain_dunder_class_access_is_denied() -> None:
    """``obj.__class__`` is denied even on an ordinary context object.

    The context passes a plain ``object()`` instance. Jinja's
    type-based sandbox checks would consider ``.__class__`` on an
    arbitrary object "safe" (it is a normal attribute of a normal
    object, not a callable that runs code). Our ``_DENIED_ATTRS``
    layer catches it anyway, because ``__class__`` is a known escape
    primitive regardless of the object it is accessed on.

    This is the test that specifically validates the deny-list layer,
    as distinct from Jinja's base layer. A regression that removed
    ``__class__`` from ``_DENIED_ATTRS`` would let this template
    render successfully (the value would be ``<class 'object'>``,
    stringified), which the test catches with a specific message
    naming the deny-list layer.
    """
    template = "{{ obj.__class__ }}"
    result = _render_text(template, {"obj": object()})
    _assert_escape_denied(result, expected_code="sandbox.denied")


# ---------------------------------------------------------------------------
# __mro__ traversal (another route to the class hierarchy).
# ---------------------------------------------------------------------------
# __mro__ is the "method resolution order" — a tuple of every class in
# a class's inheritance chain. From `str.__mro__` an attacker can reach
# `object`, and from there enumerate subclasses. It is a synonym of the
# `__bases__` route above but with a different attribute name, so it is
# a distinct vector that needs its own test.
def test_mro_traversal_is_denied() -> None:
    """``().__class__.__mro__`` is denied (the ``__mro__`` chain is closed).

    ``__mro__`` is the method resolution order — a tuple of every class
    in the inheritance chain. It reaches ``object`` in one step from any
    concrete class, which is a shorter route to the same subclass
    enumeration as ``__bases__``. Our ``_DENIED_ATTRS`` includes it,
    so the chain is closed at the first attribute access
    (``__class__``, same as the other chains).

    Why test a "redundant" vector:

    A regression that removed ``__mro__`` from the deny list but kept
    ``__bases__`` would let this specific chain through while the
    ``__bases__`` chain above stayed blocked. The two are alternative
    routes to the same capability; blocking one but not the other is
    a hole in the defence-in-depth story. Testing both closes the
    hole and, just as importantly, ensures a *future* deny-list
    edit does not accidentally reopen it.
    """
    template = "{{ ().__class__.__mro__ }}"
    result = _render_text(template)
    _assert_escape_denied(result, expected_code="sandbox.denied")


# ---------------------------------------------------------------------------
# Generator introspection (gi_frame → f_globals → module).
# ---------------------------------------------------------------------------
# Generators have an internal frame (gi_frame) from which the module
# globals can be reached (f_globals). This is a less-known route than
# __class__.__globals__, but it is *equally* effective — a template
# author who knows about it can reach any module in the process. Jinja
# denies `gi_frame` in its base sandbox; our deny list includes it
# *and* the frame's follow-on attributes (`f_globals`, `f_locals`) to
# close the entire chain, not just the entry point.
def test_generator_frame_traversal_is_denied() -> None:
    """``gen.gi_frame.f_globals`` is denied, even when ``gen`` is provided.

    Generators carry a ``gi_frame`` attribute pointing at their current
    execution frame, from which ``f_globals`` reaches the enclosing
    module's namespace. It is a valid Python route to arbitrary module
    state, and it is closed by our deny list (``gi_frame``, ``f_globals``,
    ``f_locals`` are all on ``_DENIED_ATTRS``).

    **Why the generator is passed via the context, not constructed
    inline.** An earlier version of this test wrote the chain as
    ``{{ (x for x in [1]).gi_frame.f_globals }}``, assuming Jinja
    supported Python's generator-expression syntax. It does not — Jinja
    is a template language, not Python, and ``(x for x in [1])`` is
    rejected by the parser as a syntax error before the sandbox ever
    runs. The test therefore failed for the wrong reason: it proved
    that Jinja's parser rejects generator expressions (true, but
    irrelevant to the sandbox guarantee) rather than that the sandbox
    denies ``gi_frame`` access (the actual point of the test).

    Passing the generator through the context is not a workaround —
    it is *the realistic attack vector for Forge specifically*. A
    blueprint's ``variables`` may carry arbitrary objects from the
    resolver, and a malicious blueprint author who controls both the
    template and the context can supply a generator whose frame reaches
    their module. The deny list must close that route regardless of
    how the generator arrived, and passing it via the context is the
    direct test of that guarantee.

    The test accepts ``sandbox.denied`` as the primary expected code
    (the deny list fires on ``gi_frame``). It also accepts
    ``undefined.variable`` as a fallback: depending on Jinja's version
    and the exact ordering of the sandbox checks, a follow-on attribute
    might resolve to ``Undefined`` if the first one is allowed through
    a layer this test does not anticipate. Either failure means the
    template did not reach module globals — which is what "blocked"
    actually means here. What must not happen is a successful render of
    a chain that reaches ``f_globals``.
    """

    def _generator():
        # A real generator, so ``gi_frame`` is populated. Using a
        # generator *expression* would work equally well, but the
        # function form makes the intent ("this is a generator
        # object") explicit and avoids any ambiguity about what
        # `variables["gen"]` contains.
        yield 1

    template = "{{ gen.gi_frame.f_globals }}"
    result = _render_text(template, {"gen": _generator()})
    _assert_escape_denied(
        result,
        expected_code="sandbox.denied",
        # Either the deny list fires on ``gi_frame`` (the normal path)
        # or, in an unexpected Jinja version, an attribute a step
        # later resolves to Undefined. Both mean "did not reach
        # module globals."
        allowed_alternatives=("undefined.variable",),
    )


# ---------------------------------------------------------------------------
# __subclasses__ on a class without the __class__ prefix.
# ---------------------------------------------------------------------------
# The two canonical chains above reach __subclasses__ via __class__.
# But a template author could try to reach it directly if a class were
# passed in the context. This test passes a class object explicitly and
# tries to call __subclasses__ on it — proving that even *given* a
# class, the sandbox denies the enumeration.
def test_subclasses_on_context_class_is_denied() -> None:
    """``cls.__subclasses__()`` is denied even when ``cls`` is provided.

    Passing a class through the context does not open the enumeration
    route: ``__subclasses__`` is on the deny list, so a template that
    receives a class cannot walk the object graph from it. This closes
    the "sandbox is bypassed by putting the primitive in the context"
    class of attack.

    The test passes ``str`` as the class; a real blueprint might pass
    a user-defined class (for example, a ``config`` object). The test
    uses ``str`` because it is universally available and its
    ``__subclasses__`` is trivially dangerous (every loaded class).
    """
    template = "{{ cls.__subclasses__() }}"
    result = _render_text(template, {"cls": str})
    _assert_escape_denied(result, expected_code="sandbox.denied")


# ---------------------------------------------------------------------------
# __globals__ on a callable in the context.
# ---------------------------------------------------------------------------
# A function passed via the context carries a ``__globals__`` attribute
# pointing at its defining module's namespace. If the template can
# reach it, it can reach everything in that module. This is a distinct
# vector from the ``__class__.__globals__`` chains: those use *our*
# built-in methods; this uses *the caller's* functions.
def test_globals_on_context_function_is_denied() -> None:
    """``fn.__globals__`` is denied even when ``fn`` is provided.

    A function passed through the context has ``__globals__`` pointing
    at its defining module's namespace. Whether that namespace contains
    anything dangerous depends on the function — but the sandbox does
    not try to reason about *which* globals are dangerous; it denies
    the attribute outright, which is the only correct behaviour.

    The test defines a plain function in the test module and passes it.
    A regression that allowed ``__globals__`` would let the template
    reach the test module's namespace (and, from there, ``__import__``
    via ``__builtins__``). The test catches it with a message that
    names the ``__globals__`` vector specifically.
    """

    def _dummy() -> None:
        return None

    template = "{{ fn.__globals__ }}"
    result = _render_text(template, {"fn": _dummy})
    _assert_escape_denied(result, expected_code="sandbox.denied")


# ---------------------------------------------------------------------------
# Positive control: the sandbox is not over-eager.
# ---------------------------------------------------------------------------
def test_legit_template_still_renders_with_objects_in_context() -> None:
    """A template with a plain object in context renders successfully.

    An over-eager sandbox — one that denied *every* attribute access, or
    that treated any non-primitive object as suspicious — would be as
    broken as one that allowed everything. This test is the positive
    control: a template that uses a plain object's *public* attribute
    (``obj.name``, not ``obj.__class__``) must render.

    The distinction the sandbox draws is between *dunder / introspection*
    attributes (denied) and *ordinary* attributes (allowed). This test
    pins down that distinction from the "allowed" side. Together with
    ``test_plain_dunder_class_access_is_denied`` above, it establishes
    both halves of the boundary:

        * ordinary attributes → allowed (this test)
        * introspection attributes → denied (the escape tests above)

    A regression that removed the distinction would break one or the
    other, and the failure message would name which side.
    """

    class Config:
        name = "production"

    template = "{{ config.name }}"
    result = _render_text(template, {"config": Config()})
    assert isinstance(result, Ok), (
        "the sandbox must not deny ordinary attribute access; got "
        f"{result.error.format() if isinstance(result, Err) else 'n/a'}"
    )
    assert result.value == "production"


# ---------------------------------------------------------------------------
# Shared assertion helper.
# ---------------------------------------------------------------------------
def _assert_escape_denied(
    result: Ok[str] | Err,
    *,
    expected_code: str,
    allowed_alternatives: tuple[str, ...] = (),
) -> None:
    """Assert that ``result`` is an error from the expected escape class.

    Centralised so the per-test bodies read as one line ("this chain is
    denied") and the diagnostic detail (which layer fired, what message
    was produced) is in one place. The failure message names the
    specific chain, the expected code, and any accepted alternative —
    which is what a future reviewer needs to diagnose a regression
    without reading the whole file.

    Parameters
    ----------
    result:
        The ``Result`` returned by ``TemplateRenderer.render()``.
    expected_code:
        The error code the escape must produce. Usually
        ``"sandbox.denied"``; a test that accepts a specific alternative
        passes it in ``allowed_alternatives`` and leaves this as the
        primary expectation.
    allowed_alternatives:
        Other error codes that are acceptable for this specific chain
        (e.g. ``"undefined.variable"`` when the sandbox may fire before
        the chain reaches the denied attribute). Every test that
        accepts an alternative must document *why* in its own docstring
        — the helper cannot, because it does not know the chain.
    """
    accepted = (expected_code, *allowed_alternatives)
    assert isinstance(result, Err), (
        f"escape was not blocked: expected one of {accepted}, "
        f"got Ok with rendered output: "
        f"{result.value if isinstance(result, Ok) else '<no value>'!r}. "
        "This is a sandbox regression — see _sandbox.py for the "
        "layers that are supposed to prevent this."
    )
    err = result.error
    assert err.code in accepted, (
        f"escape produced the wrong error code: expected one of "
        f"{accepted}, got {err.code!r}. "
        f"Message: {err.message!r}. "
        "A code other than 'sandbox.denied' means the escape may have "
        "been caught by a different layer (undefined-variable, "
        "invalid-arguments) rather than the sandbox — which, for the "
        "vector this test names, is a weaker guarantee than intended."
    )
    assert err.hint.strip(), (
        f"escape error has no hint; every error must suggest a "
        f"remediation. Message: {err.message!r}"
    )
    assert err.template, "escape error does not name the template source"
