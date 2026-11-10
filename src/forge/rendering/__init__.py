"""Public façade for template rendering.

This module is the *only* supported import surface for rendering. Every
consumer — scaffolding, ``forge create``, ``forge validate``, future
``forge render``, the file planner, tests — imports from here and never
from the private modules (``renderer``, ``environment``, ``_sandbox``,
``filters``, ``safe_globals``). That discipline gives us three concrete
benefits:

    1. **Refactor safety.** We can rename ``renderer.TemplateRenderer``,
       swap out the sandbox implementation, or introduce a new renderer
       for a different engine, and only this file changes. Callers are
       insulated from the churn.

    2. **Security boundary.** ``_sandbox`` and ``environment`` are
       deliberately *not* re-exported. A caller who wants a raw
       ``Environment`` has to reach into a private module, which is a
       deliberate act and shows up in code review. That is exactly the
       friction we want on security-sensitive surfaces.

    3. **Import-cost control.** Importing ``forge.rendering`` should not
       import jinja2 if the caller only wants the error type. The
       ``TemplateRenderer`` class itself is only imported on first use
       of a rendering operation — see the lazy-import pattern below.

Public API
----------
    TemplateRenderer       — the renderer class (stateless, thread-safe).
                             Exposes **two** entry points, both bound by
                             the *same* sandbox, allowlist, StrictUndefined,
                             determinism rules, and error translation:

                                 .render(path, variables)
                                     — render a file-backed template
                                 .render_inline(text, variables)
                                     — render an inline string (used by
                                       the planner for destination-path
                                       substitution; shares every
                                       guarantee of .render except that
                                       it has no filesystem reach)

                             If you change one, you cannot accidentally
                             change only the other — the shared core in
                             ``renderer._render_text`` is the reason.

    TemplateRenderError    — structured failure type

    RenderResult           — typed alias for Result[str, TemplateRenderError]
                             (see the shadowing note below for why the
                             runtime form differs from the static one)

    Ok, Err                — Result constructors, re-exported for
                             convenience so callers need one import

Not public
----------
    environment, _sandbox, filters, safe_globals, renderer
        Internal. Import from these modules is rejected in code review
        unless you are working *inside* ``forge/rendering/``.

    In particular, ``get_environment()`` is **not** re-exported. A caller
    who wants to construct their own Jinja environment is doing something
    the façade intentionally prevents — the sandbox and filter allowlist
    are policy, and policy is not a per-caller decision.

Determinism contract
--------------------
Rendering is a pure function of (template bytes, variable mapping). This
is the contract ADR-0014 establishes, and every module in this package
exists to make it true: sandboxing, allowlists, sorted context, no
time/random globals, deterministic JSON. The façade re-exports only what
callers need to honour that contract on their side (Result, error type,
renderer); it deliberately does not surface knobs that would let a
caller break it.

Why the runtime form of ``RenderResult`` differs from the static form
---------------------------------------------------------------------
Static type checkers see ``RenderResult`` as
``Result[str, TemplateRenderError]`` — a *parameterised* generic. Python's
type system erases parameterisation at runtime, so the runtime object
must be something concrete. Two options exist:

    (a) Alias to the unparameterised ``Result`` class. Simple, but
        ``isinstance(x, RenderResult)`` becomes a misleading test:
        it would be True for *any* ``Result``, including
        ``Result[int, None]`` from an unrelated module.

    (b) Alias to a ``NewType``-style runtime object that carries no
        parameterisation but *can* be used for identity comparisons
        and debugging. Overkill for a case where callers rarely
        ``isinstance``-check.

We use (a) here, because the alternative would require a runtime
``__class_getitem__`` shim and a metaclass just to make
``isinstance(x, RenderResult)`` stricter than ``isinstance(x, Result)`` —
which no caller needs, since ``Result`` is already a sum type with
exactly two members (``Ok``/``Err``) whose *inner* types are what a
caller really wants to check.

The honest statement, and the one this file makes, is: ``RenderResult``
is a **type alias** for annotation purposes, and at runtime it is the
``Result`` class itself. Callers who need to inspect a result at runtime
should use ``isinstance(result, Ok)`` / ``isinstance(result, Err)``
(already re-exported) rather than ``isinstance(result, RenderResult)``.

Shadowing note (why this file uses ``sys.modules`` instead of ``globals()``)
--------------------------------------------------------------------------
The package has a submodule named ``globals`` (the allowlist of names a
template may see, deliberately empty). Python's import machinery binds
the parent package's ``globals`` attribute to that submodule on first
import — which shadows the built-in ``globals()`` *inside this file*.
The lazy ``__getattr__`` below needs to write to the module namespace;
calling ``globals()`` there would find the submodule and fail with
``TypeError: 'module' object is not callable``.

The fix is ``sys.modules[__name__].__dict__``, which is what
``globals()`` returns, but is looked up through ``sys.modules`` and
therefore cannot be shadowed by a same-named submodule. An alternative
would be to rename the submodule, but that is a broader edit than the
problem warrants: the shadow only affects *this* file, and the
``sys.modules`` form is local, explicit, and idiomatic for exactly this
situation.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

# Eager imports: these are cheap, stable, and needed by nearly every
# consumer. `Result`, `Ok`, and `Err` come from the loader package so
# there is exactly one `Result` type in the codebase — callers never
# need to check "which Result is this?" (see ADR-0010).
from forge.loading.result import Err, Ok

# The error type is a pure dataclass with no dependencies beyond stdlib.
# Importing it is free, and every caller that handles a render result
# will need it, so it's eager.
from forge.rendering.errors import TemplateRenderError

if TYPE_CHECKING:
    # Type-only import for the alias below. At runtime this branch is
    # skipped, so importing `forge.rendering` does *not* import jinja2.
    #
    # The alias is spelled with an explicit ``alias`` so static
    # analysers see a *use* of the import inside this module, rather
    # than a re-export (which would need to be listed in ``__all__``).
    # Without this, ruff's F401 fires on the import — the alias is the
    # only consumer of the name inside this file.
    from forge.loading.result import Result as _Result


#: Typed alias for the renderer's return type. Public so callers can
#: annotate their own functions without reaching into `forge.loading`
#: and `forge.rendering.errors` separately.
#:
#:     def build(tpl: Path, vars: Mapping[str, Any]) -> RenderResult: ...
#:
#: Defined as a real runtime ``type`` alias (PEP 604 union) under
#: ``TYPE_CHECKING``, because ``from __future__ import annotations``
#: means the alias is *used* only in annotations — which are never
#: evaluated at runtime — so a ``TypeAlias``-style binding is safe and
#: gives the type checker a concrete expression to check. The previous
#: string form (``"Result[str, TemplateRenderError]"``) worked but was
#: opaque to tooling that inspects ``RenderResult.__value__``.
#:
#: At runtime the name resolves to the unparameterised ``Result`` class;
#: see the module docstring's "Why the runtime form differs" section for
#: the reasoning and for guidance on runtime isinstance checks.
if TYPE_CHECKING:
    RenderResult = _Result[str, TemplateRenderError]
else:
    # At runtime the name must still resolve for ``__all__`` and for
    # tools that introspect the module (``help(forge.rendering)``,
    # Sphinx autodoc). The runtime value is the *unparameterised*
    # ``Result`` class, which is the closest concrete equivalent
    # available without importing the generic at runtime.
    from forge.loading.result import Result as RenderResult


# The renderer class is imported lazily via a module-level `__getattr__`.
# Rationale: `TemplateRenderer` pulls in `renderer.py`, which imports
# `environment.py`, which imports `_sandbox.py` (jinja2), `filters.py`,
# and `safe_globals.py`. That is a non-trivial import chain. Consumers
# that only want to *type-annotate* against `TemplateRenderError`, or
# that import `forge.rendering` for the Result alias, should not pay for
# importing jinja2. The lazy attribute keeps startup fast and defers
# the dependency to the moment it is actually used.
_LAZY_EXPORTS = {
    "TemplateRenderer": ("forge.rendering.renderer", "TemplateRenderer"),
}


def __getattr__(name: str) -> Any:
    """Lazy-load `TemplateRenderer` on first attribute access.

    Called by Python when a name is not found in the module's globals.
    We use it to defer the (relatively expensive) `renderer` import
    until a caller actually needs the class, keeping `import
    forge.rendering` cheap for consumers that only touch
    `TemplateRenderError` or the `Result` re-exports.

    The pattern is documented in PEP 562 ("Module __getattr__ and
    __dir__"). It is a well-supported Python idiom, not a hack.

    Cache the resolved object in the module namespace after first
    access so subsequent lookups are ordinary attribute reads.

    **Why the return type is ``Any`` and not ``TemplateRenderer``.**
    Annotating it as ``TemplateRenderer`` would require importing the
    class at module scope (or under a second ``TYPE_CHECKING`` block
    just for this annotation), which defeats the *entire purpose* of
    the lazy import: the module-level ``from __future__ import
    annotations`` makes the annotation a string at runtime, but a
    type checker evaluating the signature still needs ``TemplateRenderer``
    to be in scope. Importing it under ``TYPE_CHECKING`` would be
    technically correct but would create a second, hidden import path
    that defeats the security-boundary comment above (a caller reading
    the signature sees the class name and reasonably assumes it is
    available at import time). ``Any`` is the honest type for a PEP
    562 module-level ``__getattr__``: it *can* return any attribute the
    module defines, and the type checker's job here is to permit that,
    not to over-promise. Callers who need precise typing use
    ``TemplateRenderer`` directly — the import succeeds via this very
    function, and the resulting class is fully typed.

    **Why ``sys.modules[__name__].__dict__`` and not ``globals()``.**
    The package has a submodule named ``globals`` (see the module
    docstring). Python's import machinery binds that submodule as
    ``forge.rendering.globals`` on first import, which shadows the
    built-in ``globals()`` *inside this file*. Using ``globals()``
    here would therefore find the submodule and raise
    ``TypeError: 'module' object is not callable``. Looking up the
    module namespace through ``sys.modules`` sidesteps the shadow
    because it does not go through the name ``globals`` at all.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr = target
    import importlib

    module = importlib.import_module(module_path)
    value = getattr(module, attr)

    # Cache for future lookups — this is a documented pattern; assignment
    # to the module namespace from within __getattr__ is safe here
    # because we are single-threaded during module import and the value
    # is immutable (a class object).
    sys.modules[__name__].__dict__[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy exports in `dir(forge.rendering)` for IDE support.

    Without this, `dir()` would not list `TemplateRenderer` because it
    is not in the module's globals until first access. IDEs and REPLs
    use `__dir__` to build completion lists, so listing it explicitly
    keeps discovery working without paying the import cost upfront.
    """
    return sorted(set(sys.modules[__name__].__dict__) | set(_LAZY_EXPORTS))


__all__ = [
    "Err",
    "Ok",
    "RenderResult",
    "TemplateRenderer",
    "TemplateRenderError",
]
