"""Per-file render/copy loop — the atomic unit of the staging engine.

Purpose
-------
The staging engine's *orchestration* is small (iterate the plan, emit
progress, aggregate failures, hand back a `StagingResult`). The tricky
part is the transition from *one `PlannedFile`* to *one write into
staging*. Keeping that transition in its own function pays off three
times:

    * the engine reads as a loop, not as a spool of conditionals,
    * the transition is directly unit-testable without constructing a
      plan or a destination root,
    * when the kind set grows (e.g. a future `symlink` kind for
      monorepos), only this file needs a new branch — the engine and
      its progress reporting stay untouched.

Why not render every file?
--------------------------
A blueprint can declare `kind=copy` for files that must not be
templated: binary assets (PNG, WOFF), fixtures whose content is
intentionally literal (`{{` in a JSON body that must not be
interpolated), or files owned by a vendored dependency. Running those
through Jinja would either corrupt them silently (a binary blob that
happens to contain `{{` and `}}`) or abort generation with a confusing
"undefined variable" on bytes the user never intended as a template.
The distinction is made by the *planner* (deterministic, testable) and
honoured here; this module's only job is to *act* on the decision.

Why a callback writer instead of returning bytes?
-------------------------------------------------
Returning bytes from `render_or_copy` would mean the engine — not the
staging area — holds the write handle. That would break the safety
layer's invariant that "only `StagingArea` writes into staging." Passing
the *bound* `write_bytes` method keeps the write permission scoped to
the object that owns the directory, and makes it impossible for a
caller to accidentally leak rendered content into a different path
(the bound method already knows its target root). The cost is one
parameter; the benefit is that "escapes staging" is not expressible.

Why ``render_or_copy`` and ``_render`` have a
``# type: ignore[no-any-unimported]``
------------------------------------------
``TemplateRenderer`` is imported from ``forge.rendering``, whose
façade lazy-loads the concrete class from ``forge.rendering.renderer``,
which in turn imports ``jinja2`` — a package gated behind
``ignore_missing_imports`` in ``pyproject.toml``. Mypy can therefore
see the *name* ``TemplateRenderer`` but cannot resolve its shape, and
treats any parameter annotated ``TemplateRenderer | None`` as
``Any | None``. Strict mode's ``[no-any-unimported]`` check flags
that at the ``def`` line of every function whose parameter list
contains such a type.

The suppression is narrowly scoped: it covers only
``[no-any-unimported]``, only on the affected ``def`` lines. The
parameters' *runtime* contract is unchanged — each caller passes
either a ``TemplateRenderer`` instance or ``None``, and passing
anything else fails at ``.render(...)`` with a clear
``AttributeError``. If ``jinja2`` ever gains type stubs (or the
project drops ``ignore_missing_imports`` for it), the suppressions
become unused and mypy's ``unused-ignore`` check — on under strict
mode — will force their removal in the same PR that lands the stubs.
That is the desired signal: the ignore is a *placeholder for an
external dependency's gap*, not a permanent part of this module's
contract.

Note the asymmetry with ``_copy``: that function does *not* take a
renderer, so mypy has nothing unfollowed to complain about. The
suppressions are only on the two functions whose signatures mention
``TemplateRenderer``. This is why the branch dispatcher
(``render_or_copy``) carries an ignore even though it just forwards
the renderer — the *parameter's* type is what mypy is checking, not
the body.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forge.planning import PlannedFile, PlannedFileKind
from forge.rendering import TemplateRenderer
from forge.staging.errors import Finding


# A structural type for the writer callback. Declared locally (rather
# than importing a name from `safety`) because this module is
# deliberately decoupled from the safety layer's public API: the only
# thing it needs is "something that can write bytes at a relative path".
# That keeps `_renderer_loop` testable without a real `StagingArea` and
# keeps the dependency graph one-directional.
class BytesWriter(Protocol):
    def __call__(self, relpath: str, data: bytes) -> Path:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """Result of processing exactly one `PlannedFile`.

    Kept as a frozen dataclass rather than `Result[None, Finding]`
    because:

    * the two states have *different shapes* — a success carries no
      payload, and packing "no payload" into `Ok(None)` is noise at the
      call site;
    * the field name (`finding` vs. `error`) communicates "this is a
      finding in a collected report," which is not the same as "this is
      an error that escaped a phase boundary";
    * a future variant (say, "skipped" for a kind we later add) can be
      expressed by extending this dataclass without changing its shape.

    Invariants
    ----------
    * ``ok is True`` ⟹ ``finding is None``
    * ``ok is False`` ⟹ ``finding is not None``

    The engine asserts the second invariant at the consumption site;
    this dataclass cannot enforce it directly because dataclasses lack
    cross-field constraints. Construction helpers (`success`,
    `failure`) keep the invariants obvious at every call site and make
    a future refactor that breaks them fail at the construction site.
    """

    ok: bool
    finding: Finding | None = None

    # -- constructors -----------------------------------------------------

    @classmethod
    def success(cls) -> FileOutcome:
        return cls(ok=True, finding=None)

    @classmethod
    def failure(cls, finding: Finding) -> FileOutcome:
        return cls(ok=False, finding=finding)


# ``[no-any-unimported]`` is suppressed on the ``def`` line only. See
# the module docstring's "Why ``render_or_copy`` and ``_render`` have a
# ``# type: ignore[no-any-unimported]``" section for the reasoning.
# The suppression is load-bearing for the *type checker* (which cannot
# follow ``TemplateRenderer`` through the ``forge.rendering`` façade
# into the ``jinja2``-gated module) but does not affect runtime
# behaviour: the parameter is a ``TemplateRenderer`` (or ``None``) at
# call time, and passing anything else fails at ``.render(...)`` with
# a clear ``AttributeError``.
def render_or_copy(  # type: ignore[no-any-unimported]
    planned: PlannedFile,
    *,
    blueprint_root: Path,
    variables: dict[str, object],
    write_bytes: BytesWriter,
    renderer: TemplateRenderer | None = None,
) -> FileOutcome:
    """Produce one file's bytes and write them into staging.

    Parameters
    ----------
    planned
        One entry from the plan. Its `kind` decides the branch: `COPY`
        reads and writes verbatim; `RENDER` goes through the sandboxed
        Jinja pipeline.
    blueprint_root
        Directory the blueprint was loaded from. `planned.source` is
        resolved against this — never against the process CWD, which
        would make staging sensitive to the caller's working directory
        and therefore non-deterministic across environments.
    variables
        Fully-resolved variable values. Already validated by the
        resolver; this function does not re-validate, so the cost of a
        bad value is a `RenderingError` from the template engine (with
        a clear code), not a `KeyError` from a dict lookup.
    write_bytes
        Bound writer from the staging area. The only place this
        function writes to, and the only place a caller needs to
        inspect to verify the write target.
    renderer
        Optional injected `TemplateRenderer`. The engine constructs one
        per call; unit tests can pass a stub. Defaulting to `None` and
        constructing lazily avoids paying the construction cost on
        `COPY` files, which never touch the renderer.

    Returns
    -------
    `FileOutcome.success()` on a successful write; `FileOutcome.failure(finding)`
    when the file could not be produced. Never raises: I/O errors,
    rendering failures, and missing sources are all converted to
    structured findings so the engine can aggregate them into one report.
    """
    # Resolve the source against the blueprint root, not CWD. See the
    # docstring for why.
    source_abs = blueprint_root / planned.source

    if planned.kind is PlannedFileKind.COPY:
        return _copy(source_abs, planned, write_bytes)

    return _render(source_abs, planned, variables, write_bytes, renderer)


# ---------------------------------------------------------------------------
# Branches — one per kind, kept separate so each can grow independently
# without the dispatcher accumulating special cases.
# ---------------------------------------------------------------------------


def _copy(
    source_abs: Path,
    planned: PlannedFile,
    write_bytes: BytesWriter,
) -> FileOutcome:
    """Read the source verbatim and write it to staging.

    The only transformations performed are: none. Copy-kind files exist
    precisely so that bytes the user did not intend to be templated are
    preserved exactly — a PNG with an incidental `{{` sequence, a
    fixture whose JSON body documents Jinja syntax, a vendored binary.
    Byte-for-byte preservation is tested in `test_copy_kind.py`.
    """
    try:
        data = source_abs.read_bytes()
    except FileNotFoundError:
        return FileOutcome.failure(
            Finding(
                code="copy.not_found",
                path=planned.source,
                message="source file not found",
                hint=(
                    "Check the blueprint's files[] entry: "
                    f"'{planned.source}' was not found under the blueprint directory."
                ),
            )
        )
    except IsADirectoryError:
        # A very common authoring mistake: `source:` points at a
        # directory instead of a file. Worth its own finding so the hint
        # can be precise.
        return FileOutcome.failure(
            Finding(
                code="copy.is_directory",
                path=planned.source,
                message="source path is a directory, not a file",
                hint="Point `source:` at a file, or declare each file individually.",
            )
        )
    except OSError as exc:
        return FileOutcome.failure(
            Finding(
                code="copy.io_error",
                path=planned.source,
                message=f"cannot read source: {exc.strerror or exc}",
                hint="Ensure the source is readable by the current user.",
            )
        )

    # The staging area's writer enforces its own containment; if the
    # plan promised a destination the writer cannot accept, the
    # resulting exception is a staging bug, not a template bug, and is
    # surfaced as such. The engine already validated that the
    # destination is a safe relative path during planning, so this
    # branch should never fire — keeping the guard anyway means a
    # future refactor that loosens planning cannot silently corrupt a
    # destination.
    try:
        write_bytes(planned.destination, data)
    except ValueError as exc:
        return FileOutcome.failure(
            Finding(
                code="staging.write_rejected",
                path=planned.destination,
                message=f"staging area rejected destination: {exc}",
                hint="This is a bug in the staging engine; please report it.",
            )
        )

    return FileOutcome.success()


# ``[no-any-unimported]`` on this ``def`` line only — same reasoning as
# ``render_or_copy``. See the module docstring's "Why ``render_or_copy``
# and ``_render`` have a ``# type: ignore[no-any-unimported]``" section.
# The suppression is a placeholder for jinja2's missing type stubs, not
# a permanent part of the module's contract.
def _render(  # type: ignore[no-any-unimported]
    source_abs: Path,
    planned: PlannedFile,
    variables: dict[str, object],
    write_bytes: BytesWriter,
    renderer: TemplateRenderer | None,
) -> FileOutcome:
    """Render the source as a Jinja template and write the result to staging.

    Failure modes are delegated to the renderer, which already converts
    every Jinja outcome (undefined variable, syntax error, sandbox
    denial, encoding error) into a `TemplateRenderError` with a
    structured finding shape. This function's only job is to translate
    that error into a *staging* finding (with the destination as its
    path, so users see the destination file they would have produced —
    not the template source path, which is what the Jinja error
    natively references).
    """
    # Lazy construction: copy-only plans never pay for a renderer.
    renderer = renderer or TemplateRenderer()

    result = renderer.render(source_abs, variables)
    if not result.is_ok():
        jinja_err = result.error
        # The renderer's error already contains a hint, an actionable
        # message, and (when Jinja provided it) line/column. We *also*
        # include the source path in the message, because a user reading
        # "undefined variable: foo" needs to know *which template* —
        # the destination in `path` is where the file would go, not
        # where the problem lives.
        return FileOutcome.failure(
            Finding(
                code=_staging_code_for(jinja_err.code),
                path=planned.destination,
                message=f"{jinja_err.message} (source: {planned.source})",
                hint=jinja_err.hint,
            )
        )

    try:
        write_bytes(planned.destination, result.value.encode("utf-8"))
    except ValueError as exc:
        return FileOutcome.failure(
            Finding(
                code="staging.write_rejected",
                path=planned.destination,
                message=f"staging area rejected destination: {exc}",
                hint="This is a bug in the staging engine; please report it.",
            )
        )

    return FileOutcome.success()


def _staging_code_for(rendering_code: str) -> str:
    """Map a rendering-phase code to a staging-phase code.

    Both vocabularies are documented and stable; the mapping exists so
    a consumer of `RenderingError.findings` can distinguish "the file
    failed to render" (`render.*`) from "the file could not be copied"
    (`copy.*`) from "the staging engine has a bug"
    (`staging.*`) by the code prefix alone — without needing to know
    which phase the error came from at the call site.

    Adding a rendering code without extending this map is a bug; the
    fallback `"render.error"` is safe (still actionable) but loses the
    specific classifier. Tests assert that every rendering code used in
    the renderer has a specific mapping, so this cannot silently rot.
    """
    known = {
        "undefined.variable": "render.undefined_variable",
        "syntax.error": "render.syntax_error",
        "sandbox.denied": "render.sandbox_denied",
        "include.not_found": "render.include_not_found",
        "io.encoding": "render.io_encoding",
        "io.not_found": "render.io_not_found",
        "io.error": "render.io_error",
    }
    return known.get(rendering_code, "render.error")
