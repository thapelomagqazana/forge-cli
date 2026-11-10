"""Structured template errors — the shared failure vocabulary of rendering.

Design context
--------------
Template failures are *data*, not exceptions. A batch renderer (scaffolding
a project with fifty templates) must be able to collect every failure in
one pass and hand the user a single report — not force them to fix one
error per run. The renderer therefore returns
`Result[str, TemplateRenderError]`, and this module defines what a
`TemplateRenderError` *is*: its fields, their meanings, and the invariants
that make them safe to render and safe to serialise.

Why a dedicated error type and not the loader's `ValidationError`
-----------------------------------------------------------------
The loader (`forge.loading.errors.ValidationError`) and the renderer
address different audiences and different failure surfaces:

    * The loader validates a *document*: fields, schema rules, filesystem
      references. Its natural coordinate system is the document tree
      (`variables.0.type`).
    * The renderer executes *code*: templates. Its natural coordinate
      system is a file with line/column positions, and its most common
      failure is "you used a name I don't know" — which is a runtime
      property of a template, not a static property of a document.

Forcing one type to serve both would either bloat `ValidationError` with
template-specific fields (line/column, offending name) or strip
`TemplateRenderError` of information that makes template failures
actionable. Two types, one shared `format()` convention, and a common
`Result` envelope keep the boundary clean without inventing a third
concept.

Invariants enforced by construction
-----------------------------------
1. **`hint` is mandatory and non-empty.** The whole point of returning
   errors as data is that the user can act on them. A structured error
   without a hint is a bug in the caller; making the field required at
   the type level means such a bug cannot be committed without changing
   this file (which triggers review).

2. **`code` is mandatory and machine-readable.** Human prose is not a
   stable contract; `code` is. Callers branch on `code`, never on
   `message`. This is what lets `--json` output evolve its wording
   without breaking CI.

3. **`template` is the path the user supplied**, not a temp path we
   created. Blueprint authors need to see their own path in the error,
   not an internal staging location.

4. **`line`/`col` are 1-based or `None`.** Never 0. Never -1. A `None`
   means "the failure cannot be located to a line", which is a valid
   answer for e.g. a missing top-level file.

5. **Frozen and slots.** Errors travel through the Result pipeline, get
   collected in lists, and are sometimes hashed (for deduplication in
   batch reporters). Immutability and a small memory footprint are both
   worth a one-line decorator.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TemplateRenderError:
    """A single, actionable template failure.

    Attributes
    ----------
    template:
        Path to the template as the user supplied it. Used verbatim in
        human output and JSON (`source` is deliberately *not* used here
        to match the naming users already recognise from tracebacks).

    message:
        One-sentence description of what went wrong. This is prose and
        may be reworded between minor releases; do not parse it.

    hint:
        Actionable fix. Always non-empty. Empty hint = bug in caller.

    code:
        Machine-readable class. The *stable* part of the error contract.
        Known values:

            io.not_found         template file does not exist
            io.error             template file unreadable (permissions etc.)
            io.encoding          template file is not valid UTF-8
            syntax.error         Jinja could not parse the template
            undefined.variable   a referenced name was not provided
            include.not_found    {% include %}/{% extends %} target missing
            sandbox.denied       sandbox blocked an attribute/call
            tool.error           unexpected internal failure (last resort)

        New values may be added; existing values will not be renamed
        without a major version bump of the CLI.

    line / col:
        1-based position, or `None` when the failure cannot be located.
        Populated for syntax errors and (when derivable) for undefined
        errors and encoding errors.

    name:
        The specific identifier involved in the failure when applicable
        (variable name, filter name, denied attribute). Lets downstream
        tooling highlight the exact token rather than forcing users to
        re-parse the message.

    extra:
        Reserved for callers that need to attach structured context
        (e.g. an LSP bridge attaching a byte range). Deliberately a
        plain `dict` to avoid coupling this stable type to
        caller-specific schemas. Do not put anything user-visible in
        here without a corresponding documented field.
    """

    template: str
    message: str
    hint: str
    code: str
    line: int | None = None
    col: int | None = None
    name: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    # -- validation of invariants -----------------------------------------

    def __post_init__(self) -> None:
        """Enforce invariants at construction time.

        Dataclass `__post_init__` runs on every instantiation (even with
        `slots=True` and `frozen=True`) and is the cheapest place to
        catch a caller who forgot a hint or supplied a zero-based line.
        Raising `ValueError` here is intentional: an error type that can
        itself be wrong is worse than a crash during development, which
        is when these mistakes surface.
        """
        if not self.hint or not self.hint.strip():
            raise ValueError(
                f"TemplateRenderError for {self.template!r} has an empty hint; "
                "every error must suggest a fix (see code="
                f"{self.code!r})."
            )
        if not self.code or not self.code.strip():
            raise ValueError(f"TemplateRenderError for {self.template!r} is missing a code.")
        if self.line is not None and self.line < 1:
            raise ValueError(
                f"TemplateRenderError for {self.template!r} has line={self.line}; "
                "line numbers are 1-based (or None)."
            )
        if self.col is not None and self.col < 1:
            raise ValueError(
                f"TemplateRenderError for {self.template!r} has col={self.col}; "
                "columns are 1-based (or None)."
            )

    # -- rendering helpers -------------------------------------------------

    def format(self) -> str:
        """Single-line, copy-pasteable rendering for CLI and logs.

        Kept on the type (not in a renderer) because every context that
        receives an error wants the same canonical one-line form:

            path/to/tpl.j2:12:5: undefined variable: author — hint: ...

        Contexts that want the structured fields (`--json`, LSP) read
        them directly; `format()` is the low-ceremony default.
        """
        location = self.template
        if self.line is not None:
            location += f":{self.line}"
            if self.col is not None:
                location += f":{self.col}"
        return f"{location}: {self.message} — hint: {self.hint}"

    def to_json_dict(self) -> dict[str, object]:
        """Canonical JSON representation.

        Kept here (rather than in the JSON renderer) so every consumer
        — `forge validate --json`, a future `forge render --json`, an
        HTTP endpoint — emits the same shape for the same failure. Field
        names are the *contract*; changing them requires a schema
        version bump in the DTO that wraps this dict.
        """
        return {
            "template": self.template,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "line": self.line,
            "col": self.col,
            "name": self.name,
            "extra": dict(self.extra),
        }
