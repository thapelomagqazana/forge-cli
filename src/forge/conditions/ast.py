"""Typed AST for condition expressions — the parser's output, the evaluator's input.

Why frozen dataclasses instead of Pydantic?
-------------------------------------------
* **Internal, not public.** The public contract of the conditions module
  is the *condition string* the user wrote and the *error* they get back
  when it fails. The AST is a private seam between `parser.py` and
  `evaluator.py`; exposing it publicly would tempt callers to build AST
  nodes by hand, bypassing the parser and the grammar's guarantees.
* **Cheap and hashable.** AST nodes are compared structurally in tests
  and could be cached in the future. Frozen dataclasses with `slots=True`
  are the cheapest correct implementation for that.
* **Frozen by construction.** `frozen=True` makes the AST immutable, so
  there is no path by which a later stage can mutate a node and change
  evaluation semantics out from under a downstream consumer.
* **No silent coercion.** Pydantic's value-coercion is a feature for
  external contracts and a hazard for internal ones — an AST that silently
  turns `"3"` into `3` would undermine the evaluator's type-awareness
  guarantee. Dataclasses store exactly what the parser hands them.

Design invariants (asserted in tests, documented here so reviewers can
verify them at a glance):

1. **Closed set of node kinds.** Only `Equals` and `NotEquals` exist. Adding
   a node kind requires a new file edit, a new parser branch, a new
   evaluator branch, and a new test — all visible in one PR.
2. **Literal kinds are strictly limited.** `str`, `bool`, `int` only. No
   floats (float equality is a footgun), no `None` (absence is expressed
   by variable resolution, not by a literal), no containers (a blueprint
   comparing to a list is almost certainly a bug, and it is out of the
   DSL by design).
3. **No dynamic content.** Nodes hold no reference to the original string,
   no parser state, no file handle. They are pure data.
4. **Deterministic structure.** Structural equality is the only equality —
   two nodes with the same fields are the same node, forever.
5. **Type-tag on every node.** `kind` is a string literal field so
   serialisation, logging, and future migrations can dispatch on it
   without `isinstance`, and so a node cannot be ambiguously a member of
   two kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Literal type
#
# A single, explicit alias so:
#   * parser (produces literals) and
#   * evaluator (compares literals)
# can never drift on what kinds of values are legal in an AST node.
#
# The evaluator's type-aware comparison relies on the *runtime* type being
# one of these three; if this alias ever widens (say, to include `float`),
# the evaluator's `_type_compatible` helper must be revisited in lockstep.
# Keeping the alias here makes that dependency visible.
#
# ``str | bool | int`` (PEP 604) rather than ``Union[str, bool, int]``:
# the module already has ``from __future__ import annotations``, and even
# for this runtime-evaluated alias (assignments are *not* annotations, so
# ``__future__`` does not help here), all three members are builtin types
# whose ``__or__`` is available on Python 3.10+. The project targets
# 3.11+, so the runtime form is safe — see the ``Expression`` alias below
# for the same reasoning spelled out in more detail.
# ---------------------------------------------------------------------------
LiteralValue = str | bool | int


# ---------------------------------------------------------------------------
# Operator tags
#
# Declared as module-level `Final` constants so downstream code (evaluator,
# error formatter, future SQL/JSON translators) references operators by
# *symbol* and the tag values can never be mistyped in a comparison.
# ---------------------------------------------------------------------------
OPERATOR_EQUALS: Final[str] = "equals"
OPERATOR_NOT_EQUALS: Final[str] = "not_equals"


# ---------------------------------------------------------------------------
# Node kinds
#
# `Equals` and `NotEquals` are the entire language. Do not add combinators
# (`And`, `Or`, `Not`), arithmetic, or function-call nodes without an ADR
# amendment — the value of the DSL is that its whole shape fits on this
# screen.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Equals:
    """`variable == literal` — comparison is type-aware (see evaluator).

    Attributes
    ----------
    variable
        The variable name as written in the source condition. Always a
        valid identifier, guaranteed by the parser. Stored verbatim (not
        lower-cased, not normalised) so error messages can echo the
        user's input exactly.
    literal
        A ``str``, ``bool``, or ``int`` — the *typed* literal from the
        grammar rule's family. Never the raw source text.
    kind
        Fixed tag ``"equals"``. Present so serialisation / logging /
        future migrations can dispatch without ``isinstance``.
    """

    variable: str
    literal: LiteralValue
    kind: Literal["equals"] = "equals"


@dataclass(frozen=True, slots=True)
class NotEquals:
    """`variable != literal` — the negation of `Equals` with identical
    type-awareness semantics.

    Kept as a separate node (not `Equals` + a `negated` flag) because:

    * the evaluator's branch is the natural place to encode "inequality
      of mismatched types is True" (i.e. ``"3" != 3`` is True). Two
      nodes make that rule explicit and testable;
    * error messages can name the operator directly without a lookup;
    * future tooling that walks the AST (e.g. a "collect all variables
      referenced by conditions" helper) sees the operator in the node
      shape, not in a boolean flag.
    """

    variable: str
    literal: LiteralValue
    kind: Literal["not_equals"] = "not_equals"


# ---------------------------------------------------------------------------
# Public alias
#
# The parser returns, and the evaluator accepts, `Expression`. Typing them
# against this alias (rather than `Equals | NotEquals` inline) means adding
# a node kind is a one-line change here that flows to every signature — a
# compile-time prompt to review the evaluator's exhaustive `match`/`if`
# chain.
#
# ``Equals | NotEquals`` (PEP 604) rather than ``Union[Equals, NotEquals]``:
# both members are *classes* (not ``typing`` aliases), so ``__or__`` on the
# class object produces ``types.UnionType`` at runtime on Python 3.10+.
# The project targets 3.11+, so this is safe. And unlike an annotation,
# this line is a real assignment, so ``from __future__ import annotations``
# would not postpone its evaluation even if we relied on it — the runtime
# form has to be correct on its own.
# ---------------------------------------------------------------------------
Expression = Equals | NotEquals


__all__ = [
    "OPERATOR_EQUALS",
    "OPERATOR_NOT_EQUALS",
    "Equals",
    "Expression",
    "LiteralValue",
    "NotEquals",
]
