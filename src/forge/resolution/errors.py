"""Error namespace for the resolution layer.

Why this module exists at all
-----------------------------
A one-line `from ... import ...` seems like it doesn't deserve its own
file. But it does, and here's why:

**The resolution layer must not leak its dependencies.**
`resolution.validators`, `resolution.coercion`, and `resolution.names`
all produce errors — and those errors are `forge.loading.errors.ValidationError`
instances, because the whole application speaks one error shape. If
every consumer of the resolution layer imported
`from forge.loading.errors import ValidationError`, then the resolution
layer would be *structurally coupled* to the loading layer: replacing
loading's error type would require touching every resolution consumer,
every CLI command, every test.

By routing every import through `forge.resolution.errors`, the
resolution layer owns its public error type. Today that type *is*
`loading.ValidationError` (which is correct — one error shape across the
app is a feature, not a bug). Tomorrow, if loading's error model
changes, only this file changes. Consumers of resolution keep working.

**It's the same pattern as `resolution.__init__`.**
The façade exists to control the public surface. This module does the
same job at a finer grain: it isolates the one thing every sub-module in
`resolution` needs to import, so sub-modules depend on each other
(and not on the loading layer directly).

**It gives a natural home for resolution-specific error additions.**
Today there are none — resolution reuses the loader's `ValidationError`
wholesale. But if resolution ever needs a new error class
(`VariableNotResolvableError`, `AmbiguousDefaultError`, etc.), this is
where it goes, and no consumer has to change. The pattern here is
"narrow public surface, wide private implementation": callers see
exactly what resolution wants them to see.

Guarantees
----------
* The exported `ValidationError` is *identical* to the loader's — same
  class, same behaviour, not a subclass, not a wrapper. `isinstance`
  checks work across the two layers, which is essential because
  consumers often handle errors from both (e.g. the resolver's errors and
  the loader's errors in the same batch report).

* The re-export is explicit and typed. `__all__` names the one name
  this module exports so future tooling (mypy `--strict`, reexport
  linters like `ruff`'s `TID252`) can verify the surface stays small.

* No public helper subclasses or wraps `ValidationError` "just in case".
  A parallel error type is exactly the kind of technical debt this module
  exists to prevent.
"""

from __future__ import annotations

# Re-export the canonical error shape. The identity matters: downstream
# code that does `isinstance(err, ValidationError)` must see resolution
# errors and loading errors as the same class. Subclassing or wrapping
# would break that identity and force consumers to check twice.
from forge.loading.errors import ValidationError

__all__ = ["ValidationError"]
