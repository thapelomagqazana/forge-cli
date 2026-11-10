"""Structured errors for the planning phase.

Why this file exists
--------------------
Planning is a **pure, total function**: it collects every problem it
finds and reports them together, so a user can fix a broken blueprint in
one pass instead of one issue per run. That design only works if the
error type is *data*, not an exception you raise on the first failure.

This module defines the two shapes planning exposes:

    Finding         — one issue (path-level or collision-level).
    MappingError    — an aggregate of findings from one planning run.

Both are modelled on the loader's `ValidationError` (same discipline:
path, message, hint, code) so downstream code can render planning,
loading, and rendering failures with one consistent vocabulary. Callers
that want to branch on *what kind* of problem occurred read `code`,
never parse the prose.

Relationship to other error types
---------------------------------
    forge.loading.errors.ValidationError    — blueprint/schema problems
    forge.rendering.errors.TemplateRenderError — template problems
    forge.planning.errors.MappingError      — planning problems

All three are consumed by the same `Result` at the CLI boundary, and all
three carry (path, message, hint, code). The CLI does not need a fourth
renderer per error type — it needs one "render a Finding-like object"
function. Keeping the shapes aligned is what makes that possible.

Design constraints enforced here
--------------------------------
* Findings are **immutable** — downstream code cannot rewrite them
  after collection, which prevents "silently drop this error" bugs.
* Every Finding carries a **non-empty hint** — the KPI is "0 silent
  failures", and a hint is the difference between "you're wrong" and
  "here's how to fix it".
* `MappingError` is itself immutable and structured — the CLI and tests
  can iterate `findings` without parsing `str()`, and `format()` is a
  rendering convenience, not the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Finding:
    """A single planning issue.

    Fields
    ------
    destination
        The path that triggered the issue. Empty when the issue is not
        tied to a specific destination (e.g. an empty source path, or a
        destination template that failed to render — the resolved path
        does not exist because the template is broken).

    message
        One-sentence description of what is wrong. Tells the user *what*
        without prescribing *how*; that is the hint's job.

    hint
        Actionable fix guidance. Non-empty on every finding the planner
        emits — this is the invariant the traceability KPI requires.

    code
        Machine-readable classification. Callers branch on this rather
        than on message text, so messages can be reworded without
        breaking consumers. Current codes:

            source.empty
            source.illegal_chars
            source.absolute
            source.empty_segment
            source.traversal

            destination.empty
            destination.illegal_chars
            destination.too_long
            destination.absolute
            destination.empty_segment
            destination.traversal
            destination.invalid
            destination.render_failed
            destination.collision

    sources
        Present on collision findings and on source-path findings, where
        the offending path is itself the "source". Empty for findings
        that are not about a specific source file. Preserved in planner
        order (blueprint order) for stability of the report text.
    """

    destination: str
    message: str
    hint: str
    code: str
    sources: tuple[str, ...] = ()

    def format(self) -> str:
        """Single-line rendering for logs and non-CLI consumers.

        The CLI uses its own renderer (which may add colour, indent, or
        grouping); this method is the *plain* form used in structured
        logging and any script that wants the message without colour.
        """
        location = self.destination or "<root>"
        suffix = f" [sources: {', '.join(self.sources)}]" if self.sources else ""
        return f"{location}: {self.message}{suffix} — hint: {self.hint}"


@dataclass(frozen=True, slots=True)
class MappingError(Exception):
    """Aggregated planning failures for one blueprint.

    Why `Exception` and not a plain dataclass?
        The `Result` contract requires a *value* for the error case, not
        an exception. But `MappingError` should also be raisable if a
        future caller wants exception-based handling (e.g. for a
        streaming planner that emits a plan incrementally and cannot
        return a Result at the end). Subclassing `Exception` gives us
        both modes without duplicating the type.

        This does **not** mean planning raises by default. `FileMapper.map`
        always returns `Err(MappingError(...))`; the exception base is a
        capability, not the normal flow.

    Fields
    ------
    findings
        Every problem found in the run. Never empty when raised or
        returned inside an `Err`; an empty `MappingError` would be an
        invariant violation (nothing to report means success).

    source_blueprint
        The blueprint ID the planning ran against. Included so a batch
        caller (validating fifty blueprints at once) can attribute a
        finding to its origin without threading context separately.
    """

    findings: tuple[Finding, ...]
    source_blueprint: str = "<blueprint>"

    def __str__(self) -> str:
        """Multi-line human-readable rendering.

        Format intentionally parallels `ValidationError.__str__` from the
        loader: a header with the count and source, then one indented
        block per finding. Anyone reading one kind of error already knows
        how to read the other.
        """
        lines = [f"{len(self.findings)} mapping error(s) in " f"{self.source_blueprint}:"]
        for finding in self.findings:
            location = finding.destination or "<root>"
            lines.append(f"  • {location}: {finding.message}")
            if finding.sources:
                lines.append(f"      sources: {', '.join(finding.sources)}")
            lines.append(f"      hint: {finding.hint}")
        return "\n".join(lines)

    def format(self) -> str:
        """Alias for `__str__`, for symmetry with `Finding.format()`.

        Having both forms makes calling code uniform: whether a caller
        holds a `Finding` or a `MappingError`, `obj.format()` produces a
        plain-text rendering without anyone needing to remember which
        method name belongs to which type.
        """
        return str(self)

    # ---------------------------------------------------------------------
    # Introspection helpers
    # ---------------------------------------------------------------------

    def codes(self) -> set[str]:
        """Return the distinct codes present in the findings.

        Useful for callers that want to make policy decisions ("if the
        only problems are `destination.absolute`, we can suggest a
        specific fix") without scanning the list themselves. Also useful
        in tests: asserting `codes() == {"destination.collision"}` is
        more precise than asserting "any collision present" and catches
        the bug where a run produces an unrelated finding alongside the
        expected one.
        """
        return {f.code for f in self.findings}
