"""A tiny, dependency-free `Result` type — the loader's only failure shape.

Why a hand-rolled Result instead of a library?
----------------------------------------------
The surface we actually use is small and stable:

    Ok(value)                  construct success
    Err(error)                 construct failure
    result.is_ok() / is_err()  branch on outcome
    result.unwrap()            extract value (raises on Err — loudly)
    result.unwrap_or(default)  extract value or fall back (Ok only)
    result.map(fn)             transform the Ok value
    result.map_err(fn)         transform the Err payload

Libraries like `returns`, `result`, or `rusty-results` all provide more,
but they add a dependency whose API could drift under us, and they bring
type-checker plugins that contributors must configure. A single small file
that anyone can read in thirty seconds is a better trade: no plugin
requirements, no version skew, no learning curve beyond the four methods
above. If our needs grow beyond this surface (e.g. async combinators,
traverse, applicative), swap this module for a library — call sites only
import from `forge.loading`, so the swap is contained to one file.

Design decisions worth calling out
----------------------------------
* **`@dataclass(frozen=True, slots=True)`.** Immutable and memory-efficient.
  A `Result` is a value, not a mutable holder; frozen-ness prevents
  accidental `result.value = ...` mutations that would silently rewrite
  history in a log or retry loop. `slots=True` matters because validation
  may produce thousands of `Err` instances in a batch run.

* **`unwrap_or` on `Err` raises.** This is deliberate and unusual.
  Python's `dict.get(key, default)` pattern quietly hides "key missing",
  which is fine for config lookups but wrong for validation. A caller
  writing `loader.load(path).unwrap_or(Blueprint(...))` is trying to
  bypass validation — we want that to fail in development, not silently
  ship broken blueprints to production. Callers that genuinely want a
  default must check `.is_err()` first and handle the error list, which
  forces them to think about it.

* **`map_err` on `Ok` returns `self`.** Standard functor semantics: mapping
  the error channel of a success does nothing. The type signature changes
  (`Result[T, F]`) so downstream code still type-checks.

* **No `__bool__`.** A `Result` is never truthy or falsy. Python will raise
  `TypeError: Cannot convert Ok to bool` if someone writes
  `if result:` — which is exactly what we want. The explicit `.is_ok()`
  call is the only way to branch, so a bug like "I forgot to check"
  becomes a test failure, not a production incident.

* **No `__iter__`.** Similarly, no unpacking like `ok, err = result`.
  That pattern encourages `except`-style thinking (both branches in one
  statement) instead of explicit branching. Call sites should read like
  prose: "if this succeeded, do X; otherwise, render the errors."

* **Type parameters.** `Ok[T]` and `Err[E]` are generic, and `Result` is a
  type alias `Union[Ok[T], Err[E]]`. Using `Union` rather than a class
  hierarchy means `isinstance(result, Ok)` works as a type guard for
  narrow-checkers, and `match` statements work cleanly:

      match loader.load(path):
          case Ok(blueprint):
              ...
          case Err(errors):
              ...

Compatibility
-------------
Requires Python 3.10+ (uses PEP 604 union syntax in type aliases and
`slots=True` on dataclasses, both available from 3.10). The project's
declared floor is 3.11, so this is safe.

The `Result` alias uses `TypeAlias` rather than PEP 695 `type` syntax
because PEP 695 requires Python 3.12+, and the project supports 3.11.
The `# noqa: UP040` on the alias declaration documents this decision:
the rule is suppressed, not because the modern form is wrong, but
because it would break 3.11 support. When the project's floor is
raised to 3.12, this alias (and this comment) become a candidate for
upgrade alongside every other `TypeAlias` in the codebase.

Tested by `tests/unit/loading/test_result.py` — semantics for Ok/Err,
`unwrap` raising on Err, `unwrap_or` raising on Err, `map`/`map_err`
preserving the correct branch, and equality/serialisation behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, NoReturn, TypeAlias, TypeVar

T = TypeVar("T")
U = TypeVar("U")
E = TypeVar("E")
F = TypeVar("F")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """Success case.

    Holds the value produced by a successful operation. Immutable; the
    value itself may be mutable, but the `Ok` wrapper cannot be reassigned
    to point at a different value.
    """

    value: T

    # -- branching --------------------------------------------------------

    def is_ok(self) -> bool:
        """True for `Ok`; the only supported way to branch on outcome."""
        return True

    def is_err(self) -> bool:
        """False for `Ok`. Provided so callers can write symmetric code."""
        return False

    # -- extraction -------------------------------------------------------

    def unwrap(self) -> T:
        """Return the wrapped value. Always succeeds on `Ok`."""
        return self.value

    def unwrap_or(self, default: T) -> T:  # - interface parity
        """Return the wrapped value, ignoring the default.

        `default` is unused on `Ok`; it exists so `Ok` and `Err` share
        the same method signature for callers that switch on outcome.
        """
        return self.value

    # -- transformation ---------------------------------------------------

    def map(self, fn: Callable[[T], U]) -> Result[U, E]:
        """Apply `fn` to the wrapped value, staying in the success branch."""
        return Ok(fn(self.value))

    def map_err(self, fn: Callable[[E], F]) -> Result[T, F]:
        """No-op on `Ok`. The error type parameter changes only for typing."""
        return self

    # -- representation ---------------------------------------------------

    def __repr__(self) -> str:
        # Keep the repr compact: `Ok(<value>)`. `dataclass` would default to
        # `Ok(value=<value>)`, which is noisier in logs.
        return f"Ok({self.value!r})"


@dataclass(frozen=True, slots=True)
class Err(Generic[E]):
    """Failure case.

    Holds the error payload — for the loader, a `list[ValidationError]`.
    Immutable, and no attribute named `value` exists, so code that
    accidentally reaches for `.value` on a failure fails at attribute
    lookup rather than silently getting `None`.
    """

    error: E

    # -- branching --------------------------------------------------------

    def is_ok(self) -> bool:
        """False for `Err`."""
        return False

    def is_err(self) -> bool:
        """True for `Err`; the only supported way to branch on outcome."""
        return True

    # -- extraction -------------------------------------------------------

    def unwrap(self) -> NoReturn:
        """Always raises.

        Extracting a value from a failure is a programming error: it means
        the caller forgot to check `.is_err()` first. We raise with the
        full error payload in the message so that the failing assertion in
        CI points directly at the problem rather than at a bare
        `RuntimeError('unwrap on Err')`.
        """
        raise RuntimeError(f"Called unwrap() on Err: {self.error!r}")

    def unwrap_or(self, default: T) -> T:
        """Always raises.

        Providing a default to a failed validation would silently discard
        every error the loader collected — exactly the class of bug this
        type is meant to prevent. Callers that want a default must check
        `.is_err()` and handle the errors explicitly.
        """
        raise RuntimeError(
            "Called unwrap_or() on Err. Check .is_err() and handle the "
            f"error payload explicitly. Errors: {self.error!r}"
        )

    # -- transformation ---------------------------------------------------

    def map(self, fn: Callable[[T], U]) -> Result[U, E]:
        """No-op on `Err`. The value type parameter changes only for typing."""
        return self

    def map_err(self, fn: Callable[[E], F]) -> Result[T, F]:
        """Apply `fn` to the error payload, staying in the failure branch."""
        return Err(fn(self.error))

    # -- representation ---------------------------------------------------

    def __repr__(self) -> str:
        return f"Err({self.error!r})"


# Public alias. Importers use `Result[Blueprint, list[ValidationError]]`
# in signatures; the runtime type is just a union of the two dataclasses,
# so `isinstance(result, Ok)` and `match` both work without special cases.
#
# ``UP040`` prefers PEP 695 ``type`` syntax for type aliases, which was
# introduced in Python 3.12. This project's declared floor is 3.11, so
# ``type`` syntax is not available; the ``TypeAlias`` form is the
# correct choice for the supported range. The ``# noqa`` below is
# *documentation*, not just suppression: it tells a future contributor
# exactly which constraint is gating the upgrade. When the project's
# floor becomes 3.12, remove the ``# noqa``, change the form to
# ``type Result[T, E] = Ok[T] | Err[E]``, and search the codebase for
# other ``TypeAlias`` occurrences to update alongside it.
Result: TypeAlias = Ok[T] | Err[E]  # noqa: UP040 - see comment above
