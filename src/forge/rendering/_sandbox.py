"""Forge's hardened template sandbox.

Threat model
------------
Templates are untrusted input. They arrive from blueprints that a user
may have written, cloned, or downloaded. A blueprint author's intent is
irrelevant; what matters is that a template cannot:

    * execute arbitrary code (`os.system`, `subprocess.run`, `eval`, ...),
    * read or write arbitrary files (through `{% include %}`, filters, or
      attribute chains),
    * traverse the Python object graph to reach classes, modules, or
      built-ins (`__class__`, `__subclasses__`, `__globals__`, ...),
    * escape via generator/coroutine introspection (`gi_frame`, `f_locals`),
    * mutate process state (globals, imports, sys.modules).

Layered defense
---------------
Jinja's `SandboxedEnvironment` already blocks the classic escape vectors
through `is_safe_attribute`, `is_safe_callable`, and interception of
unsafe operations. We rely on that base and add a second layer:

    1. **Deny list of attributes** (`_DENIED_ATTRS`). If a future Jinja
       release loosens its default checks, or if a novel escape route is
       published for Jinja, the deny list is our independent line of
       defence — it does not depend on Jinja's heuristics for permission.
       Denying a name is safe by construction: legitimate scaffolding
       templates never need `__class__` or `gi_frame`.

    2. **Explicit `SecurityError` on denial.** Jinja's default behaviour
       is to return `Undefined` for unsafe attributes, which surfaces as
       a confusing "attribute is undefined" error. Raising `SecurityError`
       lets the renderer emit a `sandbox.denied` code with a targeted
       hint — which is exactly what a template author attempting escape
       needs to see.

    3. **`overlay()` that re-installs allowlists.** Per-render clones
       (used to attach a blueprint-specific loader) re-run
       `filters.install()` and `globals.install()`. If a future Jinja
       version changes overlay semantics, our allowlists survive.

What this module is *not* responsible for
-----------------------------------------
    * Filter and global policy — that lives in `filters.py` and
      `globals.py`. This module only *runs* them.
    * Loader scoping — that lives in `environment.make_loader()`. This
      module only provides the `overlay()` helper used to attach it.
    * Error translation — that lives in `renderer.py`. This module only
      raises the appropriate Jinja exception (`SecurityError`), letting
      the renderer decide how to present it.

Keeping these boundaries sharp is what lets a security reviewer audit
this file in isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

if TYPE_CHECKING:
    # Type-only import: ``BaseLoader`` is the most general loader type
    # accepted by Jinja's ``Environment``, and it is what
    # ``overlay(loader=...)`` is annotated against here. Importing it
    # under ``TYPE_CHECKING`` means this module does not pay for
    # importing the loader hierarchy at import time.
    from jinja2 import BaseLoader

# ---------------------------------------------------------------------------
# Deny list
#
# Names here are attributes that a template has no legitimate reason to
# access. Each one appears in at least one published Jinja escape chain:
#
#   __class__          → ''.__class__ → str → str.__mro__
#   __mro__, __bases__ → traverse the class hierarchy
#   __subclasses__     → enumerate every loaded class, then reach os/sys
#   __globals__        → reach the function's module globals
#   __builtins__       → the builtins module, then __import__
#   __import__         → arbitrary module import
#   __loader__, __spec__ → reach importlib and execute code
#   __code__, __closure__ → access bytecode / closure cells
#   __dict__           → reach an object's attributes without methods
#   __getattribute__, __getattr__ → hook into attribute resolution
#   __setattr__        → attempt to write attributes
#
# Generator and frame introspection:
#
#   gi_frame, gi_code   → reach the frame → f_globals → module → os
#   cr_frame, cr_code   → same, for coroutines
#   f_globals, f_locals → reach locals/globals from a frame
#
# Direct module names (defense in depth — Jinja's sandbox denies
# importing these, but naming them here means even an unexpected
# reference is caught by name before type-based checks run):
#
#   os, sys, subprocess, importlib
#
# Names like `mro`, `registry` are included because Jinja's own sandbox
# maintains a list of "dangerous" attributes and these have appeared in
# past bypasses of that list; making them explicit in our deny list means
# our defense does not silently degrade if Jinja's list changes.
#
# Adding a name is a security change. Removing a name requires an ADR
# explaining what capability is being restored and why it is safe.
# ---------------------------------------------------------------------------
_DENIED_ATTRS: frozenset[str] = frozenset(
    {
        # Object graph traversal
        "__class__",
        "__mro__",
        "__bases__",
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__import__",
        "__loader__",
        "__spec__",
        "__code__",
        "__closure__",
        "__dict__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        # Generator / coroutine / frame introspection
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "f_globals",
        "f_locals",
        # Historical Jinja sandbox bypass vectors
        "mro",
        "registry",
        # Direct module names (belt-and-braces; type check should catch these
        # first, but naming them makes intent explicit)
        "os",
        "sys",
        "subprocess",
        "importlib",
    }
)


class ForgeSandboxedEnvironment(SandboxedEnvironment):
    """A `SandboxedEnvironment` with a name-level deny list and a stable overlay.

    Configuration is deliberately *not* applied here — that lives in
    `environment.get_environment()` so this class can be constructed
    without policy and instantiated in tests for isolated behaviour. The
    only decisions this class makes are:

        * which attributes are always denied (independent of Jinja's
          heuristics),
        * how denial is signalled (`SecurityError`, not `Undefined`),
        * how overlays preserve allowlists across clones.
    """

    #: No binary operators are intercepted. Jinja's sandbox already handles
    #: the dangerous cases (e.g. `__add__` on arbitrary objects); adding
    #: interceptions here would slow every arithmetic operation in a
    #: template, and no known escape requires it. If a specific escape
    #: demonstrates the need, add the operator — not a blanket interception.
    intercepted_binops: frozenset[str] = frozenset()

    #: Unary operators likewise. See above.
    intercepted_unops: frozenset[str] = frozenset()

    # -- attribute access -------------------------------------------------

    def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
        """Return True if `attr` may be read on `obj`.

        We deny by name *before* delegating to Jinja's type-based checks:

            * Jinja's checks are heuristic and version-dependent; our deny
              list is a fixed, auditable set.
            * Denying by name catches attributes that Jinja considers safe
              on a specific type — e.g. `__class__` on an object whose
              type Jinja has not yet audited.
            * Raising `SecurityError` (instead of returning False) yields
              a specific, actionable error message: "access to '__class__'
              is not allowed in templates". Returning False makes Jinja
              produce a generic `UndefinedError`, which reads as "variable
              missing" — actively misleading for a security denial.

        The `obj` and `value` parameters are unused by our check but
        kept in the signature for compatibility with Jinja's contract;
        future deny rules may need them (e.g. denying based on value
        type), and changing the signature later is a breaking change.
        """
        if attr in _DENIED_ATTRS:
            raise SecurityError(f"access to attribute {attr!r} is not allowed in templates")
        return super().is_safe_attribute(obj, attr, value)

    # -- callable access --------------------------------------------------

    def is_safe_callable(self, obj: object) -> bool:
        """Return True if `obj` may be called from a template.

        Deliberately delegates to Jinja's base implementation unchanged.
        Jinja's check is conservative (it blocks functions with dunder
        attributes, module-level functions from sensitive modules, etc.)
        and maintained against known escapes. Replacing it with our own
        would be a regression — we would lose Jinja's updates.

        The method exists explicitly (rather than being omitted) so a
        reviewer sees that *we considered* callable safety and chose to
        rely on the base. An explicit delegation is documentation of
        intent; an implicit one is not.
        """
        return super().is_safe_callable(obj)

    # -- overlay ----------------------------------------------------------

    def overlay(  # type: ignore[override]
        self,
        *,
        loader: BaseLoader | None = None,
        **overrides: Any,
    ) -> ForgeSandboxedEnvironment:
        """Return a copy with a different loader, preserving allowlists.

        The renderer calls this on every render to attach a loader scoped
        to the template's directory. Jinja's own `overlay()` already
        shares the compiled-template cache and copies configuration, but
        we:

            1. Re-install filters and globals explicitly. If a future
               Jinja version changes overlay semantics (e.g. resets
               `filters` to defaults), our allowlists survive.

            2. Assert the returned object is our class. Jinja's
               `Environment.overlay()` returns a same-class instance, but
               if that ever changes, we want to fail loudly here — the
               alternative is a subclass of `SandboxedEnvironment` that
               silently loses our deny list, which would be a security
               regression that no test would catch unless this assertion
               existed.

        **Why the `# type: ignore[override]` on the `def` line.**

        Jinja's ``Environment.overlay`` declares ~20 keyword
        parameters (``block_start_string: str = "{%"``,
        ``undefined: type[Undefined] = Undefined``, etc.). A strict
        ``[override]`` check requires this subclass to accept every
        one of them *with the exact type and default the base
        declares*. Two attempts to satisfy that literally both failed:

            * ``**overrides: Any`` alone — rejected, because mypy
              cannot prove a bare ``**kwargs: Any`` correctly binds
              the base's *named* keyword parameters. Named-parameter
              binding and ``**kwargs`` capture are different
              mechanisms in mypy's override check.

            * Re-declaring every kwarg as ``T | None = None`` —
              rejected, because it *widens* each parameter beyond
              what the base promises. A caller holding an
              ``Environment`` reference can pass ``block_start_string
              = "{%"``; my override claiming to accept only
              ``str | None`` is not a *superset* of that call, it is
              a different (and narrower by type) one. Re-declaring
              them with the base's *exact* types and defaults would
              work, but would freeze Jinja's constructor surface in
              this file — every Jinja minor release that adds a
              kwarg would produce a new error here, for a signature
              this file does not own.

        The chosen shape — a keyword-only ``loader`` (the only
        parameter this method is *called with* in this codebase)
        plus ``**overrides: Any`` — accepts every call the base
        accepts at *runtime*. mypy cannot verify that statically for
        the reasons above, so the check is suppressed here, once,
        with this explanation. The runtime behaviour is exactly
        correct: ``super().overlay(loader=loader, **overrides)``
        forwards every kwarg the base declares, and the base's own
        signature validation does the rest.

        **Why this is safe despite the suppression.** The suppression
        covers *parameter binding* only. It does not weaken:

            * The *return type* (``ForgeSandboxedEnvironment``), which
              mypy still checks against the base's return type.
            * The ``isinstance`` assertion below, which fails loudly
              if a future Jinja release ever returns a non-Forge class
              from ``overlay()`` — the security-relevant property this
              method exists to protect.
            * Any other override in this class (``is_safe_attribute``,
              ``is_safe_callable``), which are fully type-checked.

        A reviewer verifying this file should confirm three things:
        (a) the override suppresses only ``[override]``, not
        ``[override]`` *plus* other codes; (b) the body forwards
        ``loader`` and ``**overrides`` verbatim to the base; (c) the
        ``isinstance`` assertion is present and raises on mismatch.
        All three are checkable in isolation, without needing to
        compare ~20 kwarg signatures by eye.

        **Why ``loader`` is keyword-only.** ``overlay()`` is called on
        every render. Making the loader keyword-only prevents the
        "oops, passed the loader as the first positional" mistake,
        which would otherwise silently bind to ``block_start_string``
        inside Jinja's own ``overlay``. That footgun is real, and the
        ``*`` costs nothing at the call sites that already pass
        ``loader=...`` explicitly.

        **Why ``loader`` defaults to ``None``.** The base signature
        is ``loader: BaseLoader | None = None``; ``None`` means "keep
        the parent's loader." A caller who does not pass a loader
        gets the parent's — the correct semantics for an overlay.
        """
        from forge.rendering import filters
        from forge.rendering import globals as globals_mod

        clone = super().overlay(loader=loader, **overrides)

        if not isinstance(clone, ForgeSandboxedEnvironment):
            raise RuntimeError(
                "Sandbox overlay returned an unexpected class "
                f"({type(clone).__name__}); refusing to continue because "
                "the deny list may not be enforced on the clone."
            )

        # Re-install allowlists on the clone. `install()` clears then
        # updates, so this is idempotent whether or not `overlay()`
        # already copied the parent's filters/globals.
        filters.install(clone)
        globals_mod.install(clone)

        return clone
