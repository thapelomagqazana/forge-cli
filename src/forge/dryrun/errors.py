"""Dry-run errors — the *data* half of the dry-run contract.

Two audiences, one type
-----------------------
The dry-run service returns ``GenerationPlan | DryRunError``. A
``DryRunError`` is not an *exceptional* condition in the Python sense —
it is the other branch of a total function, just like ``Err`` is in the
loader's ``Result``. This module therefore models it as a **value**
(``frozen dataclass``, comparable, serialisable), not a raisable
exception. It happens to subclass ``Exception`` only so that callers
that prefer ``raise`` semantics can adopt them without changing types;
the service itself returns, never raises.

Why a ``findings`` list, not a single message
---------------------------------------------
A dry-run can fail for *many* reasons at once — three conditions
reference undefined variables, two mappings collide with disk, one
destination is unresolvable. A tool that reports one failure per run
makes the user re-run three times to fix one blueprint. The whole point
of aggregated findings (established in ADR-0010 for the loader, ADR-0015
for mapping, ADR-0016 for conditions) is: **one report, complete**.
``DryRunError`` inherits that discipline, so a user fixes every problem
in one edit.

Why the fields are what they are
--------------------------------
``Finding.path``
    The thing the user can point to. For a condition error, that is the
    condition string verbatim (``"database == 'postgresql'"``). For a
    mapping error, it is a destination or a source path. Never an
    internal pointer — findings are for humans first, scripts second.

``Finding.code``
    A stable, machine-readable category (``condition.undefined_variable``,
    ``destination.collision``, ``plan.case_insensitive_duplicate``).
    Callers that branch on errors (CI integrations, future ``--strict``
    modes) key off ``code``; callers that *read* errors key off
    ``message`` and ``hint``. Separating these two concerns is what lets
    us reword a message without breaking automation.

``Finding.hint``
    A concrete next step. *"Undefined variable"* is a diagnosis;
    *"add 'database' to the blueprint's variables, or provide a value
    at create time"* is a fix. Every ``Finding`` in this codebase
    carries a hint — a ``Finding`` without one is considered a bug
    (enforced by tests across modules).

``DryRunError.operation``
    Which phase of the dry-run failed (``"dry-run:conditions"``,
    ``"dry-run:mapping"``). This is critical because it tells the user
    *which* part of their blueprint to fix first: condition errors mean
    the plan cannot even be computed, while mapping errors mean the
    plan is incomplete. Fixing a mapping error while a condition error
    is present is wasted effort — ``operation`` makes that ordering
    visible.

What this module deliberately does **not** contain
--------------------------------------------------
* **No exit codes.** ``ExitCode.DRY_RUN_COLLISIONS`` is a *CLI*
  concern (see ``forge.cli.errors``). Embedding exit codes in the DTO
  would couple the domain to the interface; the CLI maps findings to
  codes, not the other way around.

* **No renderers.** Human and JSON renderers are pure functions in
  ``forge.dryrun.renderers``. This module is data; renderers are
  presentation. Mixing them would break the "same DTO for both
  audiences" contract (see ADR-0012 for the pattern).

* **No ``__str__`` that assembles a full report.** A raw
  ``__str__`` that dumps every finding produces a string that is
  *almost* the human renderer but not quite — and diverges over time.
  The renderers are the only place a ``DryRunError`` gets formatted for
  humans. ``__str__`` here is a minimal diagnostic (one line per
  finding) intended for logs and REPL exploration, not for end-user
  display.

* **No cause chaining.** We do not store the original exception that
  motivated a finding (e.g. an ``OSError`` from ``lstat``). The
  ``message`` already includes the relevant detail, and keeping the
  original exception alive would (a) keep stack frames in memory for
  every finding, and (b) tempt callers to unwrap and re-raise, which
  would defeat the aggregated-report contract. Logs that need the
  original exception capture it at the point of failure, not here.

Testing contract
----------------
Every ``DryRunError`` produced by ``DryRunService.plan()`` in the test
suite is asserted to:

    * be a ``DryRunError`` instance,
    * have ``len(findings) >= 1``,
    * have every finding carry a non-empty ``path``, ``message``,
      ``hint``, and ``code``,
    * name an ``operation`` that matches the phase that failed.

That test lives in ``tests/unit/dryrun/test_errors_contract.py`` and is
the enforcement mechanism for everything above being true (rather than
merely *documented* as true).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Finding — the atom of a report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """A single, actionable problem uncovered while planning a dry-run.

    This type is defined here (not imported from ``forge.conditions`` or
    ``forge.planning``) so the ``DryRunError`` contract has its own,
    independent shape. The fields happen to align with those modules'
    findings today — but the alignment is a *choice*, not a coupling.
    If either upstream module adds or renames a field, the dry-run
    contract is unaffected.

    Attributes
    ----------
    path
        Where to look. For condition errors, the condition string as
        written in the blueprint. For mapping/safety errors, a source
        or destination path. Never an internal pointer.
    message
        What is wrong, in one sentence. Present tense, active voice.
    hint
        What to do about it. Must be actionable; *"invalid value"* is
        not a hint, *"use one of: true, false"* is.
    code
        A stable, dotted, machine-readable category. Callers that
        branch on findings key off this; wording can change without
        affecting them.
    """

    path: str
    message: str
    hint: str
    code: str


# ---------------------------------------------------------------------------
# DryRunError — the aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DryRunError(Exception):
    """All findings that prevented a plan from being produced.

    Subclasses ``Exception`` for compatibility with callers that prefer
    ``raise`` semantics, but the dry-run service *returns* this value
    rather than raising it (see module docstring). Callers should treat
    it as the second branch of ``GenerationPlan | DryRunError``.

    Attributes
    ----------
    findings
        The complete list of problems. Never empty — an empty findings
        tuple would be a bug in the caller (the service returns a
        ``GenerationPlan`` in that case). Enforced by tests.
    operation
        The dry-run phase that failed, prefixed with ``"dry-run:"``
        (``"dry-run:conditions"``, ``"dry-run:mapping"``). Aids triage
        and tells the user which part of the blueprint to fix first.
    """

    findings: tuple[Finding, ...]
    operation: str = "dry-run"

    # ------------------------------------------------------------------
    # Presentation (log/diagnostic only — see module docstring)
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        """One line per finding, plus a header.

        Deliberately terse: this is what a ``logging.error(exc)`` call
        produces, or what a REPL prints when a user explores the error
        interactively. It is **not** the human renderer — that role is
        reserved for ``forge.dryrun.renderers.render_human``. Keeping
        the two separate means a reword of the human renderer does not
        churn log output (and vice versa).
        """
        n = len(self.findings)
        header = f"{n} dry-run error(s) during {self.operation}:"
        lines = [header]
        for f in self.findings:
            lines.append(f"  [{f.code}] {f.path}: {f.message}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Convenience (pure; no I/O, no mutation)
    # ------------------------------------------------------------------

    def codes(self) -> tuple[str, ...]:
        """Return the distinct ``code`` values present in ``findings``.

        Sorted for determinism (matching the codebase's "all collections
        are ordered" discipline). Useful for CI scripts that want to
        branch on *which classes* of problem occurred without parsing
        the ``findings`` list itself.
        """
        return tuple(sorted({f.code for f in self.findings}))

    def has_code(self, code: str) -> bool:
        """True if any finding has the given ``code``.

        A one-line predicate beats ``any(f.code == code for f in
        err.findings)`` at every call site — and the name reads like
        English, which is what we want for the most common CI query
        (``if err.has_code("destination.collision"): ...``).
        """
        return any(f.code == code for f in self.findings)
