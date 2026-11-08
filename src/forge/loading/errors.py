"""The one validation-error shape the whole application speaks.

Why a dedicated module for one dataclass?
-----------------------------------------
A `ValidationError` is the *contract* between the loader and every
consumer: the CLI renderer, the `forge validate` command, discovery's
warning logger, future HTTP endpoints, future IDE integrations. If any
consumer re-invented "an error shape" with slightly different fields, we
would need translation layers at every boundary — exactly the technical
debt this pipeline is designed to avoid.

Keeping the shape in its own module also means:

* `loading/hints.py` can import `ValidationError` for helper return types
  without pulling in the loader (avoiding an import cycle when the loader
  imports hints).
* Tests can construct `ValidationError(...)` fixtures directly, with no
  loader machinery in the loop.
* A future renderer (e.g. `--format=json`) can serialise *this* class, and
  every error in the system renders identically because they all come
  through this type.

Field-by-field rationale
------------------------
Every field earns its place. Removing any of them breaks a real consumer:

    path      → the JSON-Pointer-derived dotted path. The CLI prints it,
                CI tooling keys on it (e.g. "fail if path starts with
                'schema.'"), and tests assert against it. No consumer can
                substitute another field for this.

    message   → the *what*. A single sentence. Never contains a hint
                (mixing the two makes "print just the problems" queries
                lose the hint, and "print just the hints" queries lose
                the problem). One responsibility per field.

    hint      → the *what to do*. Mandatory (empty string is a bug).
                This is the field that determines whether a user fixes
                the error in 10 seconds or 10 minutes. Enforcing
                non-emptiness at the loader level (via the hint catalogue
                in `hints.py`) is what makes the KPI "100% of errors
                carry a hint" a fact rather than an aspiration.

    source    → the input label. Required because errors are frequently
                rendered in a batch — `forge validate examples/*.yaml`
                must be able to say *which file* failed. In-memory
                callers pass "<memory>" so the field is never None.

    line      → present iff the YAML parser or a future line-aware
    column      validator could locate the failure. Optional because
                semantic errors (missing template, semantic invariant)
                have no meaningful line — inventing one would lie.

    extra     → a small typed escape hatch for phase-specific data
                (e.g. `{"phase": "schema"}`). Kept as `dict[str, object]`
                rather than `dict[str, Any]` so downstream code can still
                type-narrow the value; not used by the CLI today, but
                allows "render error details as JSON" to work without
                a format change later.

Deliberately NOT included:

    severity  → every error from the loader is fatal for the load. If
                warnings were ever needed (e.g. "your version is
                deprecated"), they belong in a separate `Warning` type
                that the loader returns alongside the Result, not mixed
                into the error list. Mixing severities would force every
                consumer to filter, and filtering is exactly how warnings
                get lost.

    code      → tempting for i18n, but every consumer today uses the
                message. Adding codes speculatively means we have to keep
                them stable forever without any real benefit. If a
                structured-errors use case arises, `extra["code"]` is
                the migration path.

    exception → storing the underlying exception object breaks the
                frozen/slots contract and leaks implementation details
                to callers. Callers who need a stack trace can log at the
                raise site, not in the error value.

Frozen and slotted
------------------
`@dataclass(frozen=True, slots=True)` because:

* `frozen` → errors are values. A renderer that mutates `error.message`
  while formatting is a bug; frozen makes that impossible.
* `slots`  → batch validation of thousands of files can produce
  thousands of these; `slots` cuts per-instance memory meaningfully.
  Also prevents accidentally stashing a stray attribute on an error
  while debugging.
* Together they make `ValidationError` hashable, which means the loader
  can dedupe repeated errors (e.g. the same hint path triggered by two
  records) without a custom `__hash__`.

Equality and ordering
---------------------
`dataclass(frozen=True)` gives value equality for free — two identical
errors compare equal, which is what tests want. No `__lt__` is provided:
ordering is the *loader's* concern (it sorts by path), not the error's.
Errors represent facts; the ordering of facts is presentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A single, actionable validation failure.

    All instances of this class are produced by the loader pipeline
    (`parse_yaml`, `schema_errors`, `model_errors`, `check_templates`)
    or by helpers in `loading/hints.py` that construct them for the
    "unknown field" and "unknown phase" cases. Consumers should treat
    this class as closed: they render and route it, they do not extend it.
    """

    path: str
    """Dotted field path, e.g. `variables.0.type`.

    Derived from JSON Pointers (`/variables/0/type`) so it lines up with
    the schema's own error paths and with any JSON tooling the user may
    run on the same document. An empty string means "the root document
    itself", which occurs for parse errors and file-level read failures.
    """

    message: str
    """One-sentence description of what is wrong.

    Convention: starts lowercase (it will often be embedded in a
    sentence like "{source}: {path}: {message}"), no trailing period,
    no hint text. Reports the *problem*, not the fix.
    """

    hint: str
    """What the user should do about it.

    Guaranteed non-empty by convention (empty hints are a bug and are
    caught in tests). Contains an imperative suggestion — "Use full
    SemVer, e.g. '1.0.0'" — not a repeat of the message.
    """

    source: str = "<memory>"
    """Label identifying the input this error came from.

    For file-backed loads, this is the path as the caller provided it
    (not `resolve()`d — that would mislead users about symlinks and
    relative paths). For in-memory validation, callers pass a label
    that makes sense in their context (e.g. `<test:multi-error>`), and
    the default covers the anonymous case.
    """

    line: int | None = None
    """1-based line number, when the phase could locate it.

    Only YAML-parse errors populate this today. Schema and model errors
    are reported by dotted path (which is what users edit by), and
    synthesising a line number from the path would require round-tripping
    through a YAML node graph that `safe_load` does not preserve.
    """

    column: int | None = None
    """1-based column number, when the phase could locate it.

    Paired with `line`. Both are populated together or not at all; a
    single-sided location is more confusing than useful, so future
    phases must respect this pairing.
    """

    extra: dict[str, object] = field(default_factory=dict)
    """Phase-specific structured payload (e.g. `{"phase": "schema"}`).

    `default_factory=dict` rather than `default={}` — the classic mutable
    default trap. Because the dataclass is frozen, the dict itself is
    still reachable (frozen only prevents rebinding the attribute), so
    callers must not mutate it. Treat it as read-only. Kept small and
    typed loosely on purpose: adding a new phase with a new key must not
    require touching this class.
    """

    # ------------------------------------------------------------------
    # Presentation helpers
    #
    # Kept on the class rather than in a separate formatter because the
    # two most useful renderings — single-line and multi-line — are
    # universally needed (CLI, logs, error pages, test assertions) and
    # putting them here means every consumer sees the same text. A
    # future `--format=json` renderer will serialise the fields directly
    # and is not affected by these helpers.
    # ------------------------------------------------------------------

    def format(self) -> str:
        """Single-line, copy-pasteable rendering.

        Example outputs::

            broken.yaml:3:12: invalid YAML: found unexpected ':'
                — hint: Re-save the blueprint as UTF-8 ...

            examples/multi-error.yaml: version: expected '1.0.0',
                got '1.0' — hint: Use full SemVer, e.g. '1.0.0'.

        The `line:column` prefix is included only when both are present,
        because a half-located error (`broken.yaml:3`) is more confusing
        than an unlocated one.

        The path appears as `<root>` for empty paths so a user does not
        see `::  invalid YAML` (which looks like a formatting bug).
        """
        if self.line is not None and self.column is not None:
            location = f"{self.source}:{self.line}:{self.column}"
        else:
            location = self.source
        field_path = self.path or "<root>"
        return f"{location}: {field_path}: {self.message} " f"— hint: {self.hint}"

    def format_multiline(self) -> str:
        """Two-line rendering suitable for narrow terminals and JSON logs.

        Line one carries location + path + message; line two carries the
        hint indented under the first. The CLI uses `format()` (compact,
        one line per error, greppable); human-facing summary screens use
        this method when the terminal is narrow or when errors are few.

        Kept on the class rather than in the CLI so both renderings stay
        in sync when the field set changes.
        """
        if self.line is not None and self.column is not None:
            location = f"{self.source}:{self.line}:{self.column}"
        else:
            location = self.source
        field_path = self.path or "<root>"
        return f"{location}: {field_path}: {self.message}\n" f"    hint: {self.hint}"
