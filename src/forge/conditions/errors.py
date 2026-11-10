"""Structured errors for the condition subsystem.

Why a dedicated module?
-----------------------
Every subsystem in Forge (loading, resolution, planning, rendering) has
one job and one error shape. The conditions module's job is *evaluation of
a restricted DSL*. Its errors are therefore not "exceptions the parser
raises" — they are **data** that the caller aggregates with the rest of
the pipeline's findings and prints in one report. That is why:

* ``Finding`` is a frozen dataclass, not an ``Exception`` subclass. It is
  a *record of a problem*, not a signal to unwind the stack.
* ``ConditionError`` is an exception, because it is the *aggregate* that
  a command raises once at the boundary (``raise SystemExit(1)``). It
  holds a tuple of findings, not a message.
* The shape is deliberately a superset of the loader's
  ``ValidationError.format()`` output: same discipline ("path, message,
  hint, code"), same rendering style, so the CLI's error renderer can
  handle both.

Contract
--------
A ``ConditionError`` guarantees:

1. **Non-empty.** Constructing one with no findings is a programming
   error (asserted in ``__post_init__``).
2. **Structured.** Every finding carries ``condition`` (a representation
   of the offending source — see ``Finding.condition`` for the precise
   guarantee, which differs between producers), ``message`` (what went
   wrong), ``hint`` (what to do), and ``code`` (machine-readable class).
   No finding is missing any of these — asserted in tests.
3. **Actionable.** Hints come from the parser and evaluator; both are
   curated strings reviewed with the same care as user-facing CLI text.
   Empty hints are a defect, not a degenerate case.
4. **Deterministic.** ``__str__`` is stable across runs; iteration order
   matches insertion order (which the evaluator controls: declaration
   order of the blueprint's conditional groups).
5. **Serialisable.** ``to_dict()`` produces a JSON-safe structure so the
   CLI's ``--json`` mode can reuse it without a bespoke adapter.

Deliberate non-goals
--------------------
* **No exception hierarchy.** A single ``ConditionError`` covers both
  parse-time and evaluation-time failures. Splitting them into
  ``ParseError``/``EvalError`` classes would force callers to catch two
  things to handle "the conditions were wrong", which is never how a
  caller thinks about it.
* **No message field on ``ConditionError`` itself.** The message is
  derived from findings in ``__str__``. Storing a message and findings
  separately invites them to drift.
* **No I/O, no formatting that depends on terminal width, colour, or
  locale.** Rendering is the CLI's job (per ADR-0012); this module
  produces the raw, structured data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Finding — one problem, atomically.
#
# Design notes
# ------------
# * ``condition`` holds a *representation* of the offending condition, not
#   a source-of-truth locator. Two producers populate it differently and
#   the difference is load-bearing — see the field docstring below. A
#   previous revision of this file claimed the field was "stored
#   verbatim" for every finding; that claim was true when the parser was
#   the only producer and became false when the evaluator began
#   reconstructing a canonical form from the AST. The current docstring
#   describes both behaviours accurately, which is what the tests now
#   verify.
# * ``code`` is a *machine-readable* class, not a human sentence. It is
#   stable across releases and used by tests, CI filters, and future
#   documentation generators. Human wording lives in ``message``/``hint``.
# * ``slots=True`` because findings can appear in large numbers during
#   batch validation; the memory footprint matters at scale and slots are
#   free to add.
# * ``frozen=True`` so a finding cannot be mutated after it leaves the
#   evaluator (invariant 3 in ast.py: errors, like ASTs, are data).
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Finding:
    """One structured problem discovered while parsing or evaluating.

    Attributes
    ----------
    condition
        A representation of the offending condition, chosen by the
        producer. Two producers exist today, and they represent the
        condition differently on purpose:

        * **Parser findings** (``ParseError.to_finding()``) echo the
          raw source text *verbatim*, including whitespace and quote
          style. Rationale: a parse error is "this specific text
          doesn't parse", and echoing the exact text is the most
          useful feedback the parser can give.

        * **Evaluator findings** (``ConditionalEvaluator._evaluate``)
          reconstruct a *canonical* form from the AST (single spaces
          around the operator, single quotes around strings). The AST
          itself holds no reference to user input — invariant 3 in
          ``ast.py`` — so the evaluator cannot echo the raw source even
          if it wanted to. The canonical form has two benefits for the
          consumer: identical errors for semantically identical
          conditions (whitespace-insensitive diffs and snapshots), and
          a form that matches the ``CONDITION_GRAMMAR.md`` examples
          users are reading alongside the error.

        Both representations preserve every semantic element — the
        variable, the operator, and the literal — so a user reading
        the finding sees what they wrote in substance, and sees a
        stable, comparable form for scripting and CI.

        Callers that need the raw source text (e.g. an editor plugin
        that highlights the exact characters) should read it from the
        blueprint itself, not from the finding. The finding is a
        *report*, not a source-of-truth locator. If verbatim source
        becomes a hard requirement for evaluator findings, the fix is
        to add a separate ``source_text: str | None`` field populated
        only by producers that *have* the raw text, not to break the
        AST's purity invariant by smuggling the raw string into every
        node.
    message
        One-sentence description of what went wrong, in the voice the
        rest of the CLI uses ("undefined variable: database").
    hint
        Actionable suggestion. Non-empty for every finding. Tells the
        user *what to change*, not merely what is wrong.
    code
        Stable, machine-readable classifier. Current values are:

        ``condition.empty``
            The condition string was missing or whitespace-only.
        ``condition.syntax``
            The string did not match any rule in the grammar.
        ``condition.invalid_variable``
            The variable name violated the identifier grammar.
        ``condition.undefined_variable``
            The variable was well-formed but not present in the resolved
            variable set.
        ``condition.internal``
            A defect in the conditions module itself (defensive only).
    """

    condition: str
    message: str
    hint: str
    code: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-safe representation for ``--json`` output.

        Kept explicit so a future field addition is a one-line change
        here rather than a silent leak into the public contract via
        ``dataclasses.asdict`` (which would include any future internal
        fields we add and later regret exposing).
        """
        return {
            "condition": self.condition,
            "message": self.message,
            "hint": self.hint,
            "code": self.code,
        }


# ---------------------------------------------------------------------------
# ConditionError — the aggregate raised at the boundary.
#
# An *exception* because it is unwound to ``SystemExit`` by the CLI. But
# it behaves like a value: immutable, structured, deterministic string
# form. This is deliberate: the CLI's error handler is a single
# ``except ConditionError as e: print(e.format()); sys.exit(2)``
# and never inspects ``args`` or parses the message.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConditionError(Exception):
    """Aggregate of every condition finding for one blueprint.

    Attributes
    ----------
    findings
        Non-empty tuple of findings, in the order the evaluator produced
        them (which is the blueprint's declaration order for
        ``conditional_files`` groups). Order is preserved so the CLI
        output is deterministic and diffable in CI.
    source_blueprint
        The blueprint id, used in the aggregate header and JSON output.
        Defaults to ``"<blueprint>"`` so a hand-built error (in tests)
        still renders sensibly.
    """

    findings: tuple[Finding, ...]
    source_blueprint: str = field(default="<blueprint>")

    def __post_init__(self) -> None:
        # Enforce the "non-empty" invariant at construction. This is a
        # programming error, not a user error, so it raises immediately
        # rather than producing an unhelpful "0 errors" report.
        if not self.findings:
            raise ValueError("ConditionError requires at least one finding")

    def __str__(self) -> str:
        """Deterministic, human-readable aggregate.

        Matches the style of the loader's ``ValidationError.format()``
        so the CLI's error renderer can present both with one code path.
        The exact wording is *not* a public contract — automation should
        consume ``to_dict()`` — but it is stable across runs and clear
        enough for humans to act on.
        """
        header = f"{len(self.findings)} condition error(s) " f"in {self.source_blueprint}:"
        lines: list[str] = [header]
        for finding in self.findings:
            lines.append(f"  • {finding.condition!r}")
            lines.append(f"      {finding.message}")
            lines.append(f"      hint: {finding.hint}")
        return "\n".join(lines)

    def format(self) -> str:
        """Alias for ``__str__`` to mirror the loader's public API.

        Callers that render errors uniformly (``err.format()``) do not
        need to know which subsystem produced the error. Keeping the
        alias here costs nothing and unifies the CLI.
        """
        return str(self)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe representation for ``--json`` mode.

        Envelope shape mirrors the loader's and the planner's JSON
        outputs: a stable object with a schema version, the source
        identifier, and a list of structured findings.
        """
        return {
            "schema_version": "1.0.0",
            "source": self.source_blueprint,
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def single(
        cls,
        finding: Finding,
        *,
        source_blueprint: str = "<blueprint>",
    ) -> ConditionError:
        """Ergonomic constructor for the common one-finding case.

        Used by tests and by callers that handle findings one at a time
        (e.g. an interactive wizard that wants to surface the first
        problem immediately). Kept as a classmethod so callers do not
        have to construct a one-element tuple by hand and risk the
        ``(finding)`` tuple-of-string footgun.
        """
        return cls(findings=(finding,), source_blueprint=source_blueprint)


__all__ = ["ConditionError", "Finding"]
