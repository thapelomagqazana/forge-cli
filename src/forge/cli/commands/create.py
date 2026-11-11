"""``forge create`` — create a project from a blueprint.

This command is the primary entry point for project generation. It
takes a blueprint ID (e.g., ``python.fastapi.api``), resolves it from
the discovered blueprints, resolves the variables (from CLI flags,
interactive prompts, or blueprint defaults), evaluates conditional
file groups against those variables, plans the file manifest, validates
the plan against the destination filesystem, and commits the project
atomically through a staging directory.

The seven-phase pipeline
------------------------
Every ``forge create`` invocation walks these phases in order. Each
phase either returns a ``Result`` or raises a typed error the CLI
handler classifies.

    1. **Load** the blueprint document (``get_blueprint_document``).
       Fails with exit code 2 on malformed/missing blueprints.
    2. **Resolve** project name and variables (``VariableResolver``).
       Fails with exit code 1 on user input errors.
    3. **Evaluate** conditional file groups against the resolved
       variables (``ConditionalEvaluator``). Fails with exit code 1 on
       broken conditions.
    4. **Plan** the file manifest (``FileMapper``). Fails with exit
       code 1 on collisions or unsafe destinations.
    5. **Validate** the plan against the filesystem (``forge.safety``).
       Fails with exit code 3 on structural refusals, exit code 6 on
       collisions.
    6. **Back up** existing files if ``--overwrite`` was passed
       (``forge.safety.backup_existing``). Fails with exit code 3 on
       any I/O error, and rolls back partial backups so the
       filesystem is left exactly as it was at phase entry.
    7. **Stage and commit** (``StagingArea``). Renders each planned
       file into a staging directory, then atomically renames them
       into place. Fails with exit code 4 on template errors.

The ordering is not incidental. Phase 5 is a *pre-flight* — it
validates the plan before any file is touched, so a plan with a
traversal attempt fails without ever opening a write handle. Phase 6
is a *data-preservation* gate — it copies every file about to be
replaced before any write occurs, so an interrupted phase 7 leaves
the originals intact. Phase 7 is the *only* phase that mutates the
destination, and it does so through ``StagingArea`` — the same
guarantee ADR-0017 makes: *no write escapes staging*.

Dry-run
-------
When ``--dry-run`` is passed, phases 1 to 4 run normally (they are pure
and read-only) and phases 5-7 are replaced by a single call to
``DryRunService.plan``. The service composes exactly the same
components (``FileMapper``, ``ConditionalEvaluator``,
``detect_collisions``) into a versioned ``GenerationPlan`` DTO, which
is then rendered by the human or JSON renderer. Two paths, one
pipeline, one contract — see ADR-0018.

Dual-mode input handling
------------------------
The command supports two input modes, dispatched once in
``_resolve_inputs`` (see ADR-0006):

- **Non-interactive** — ``--no-interactive`` was passed, or stdin is
  not a TTY. All required values must come from flags. If any are
  missing, the command fails *before any side effects* with a single
  ``MissingRequiredFlagsError`` listing every gap.

- **Interactive** — stdin is a TTY and ``--no-interactive`` was not
  passed. Any value not supplied as a flag is collected by the
  wizard in ``cli.prompts``. Flags win. Ctrl+C at any prompt raises
  ``PromptCancelledError``, which the CLI handler renders as a
  dimmed "Cancelled." line and exits with code 1.

Both modes converge on ``VariableResolver.resolve`` for *blueprint
variables*. The project name is a *command argument*, not a blueprint
variable, and is validated separately — but by the *same* named
validator (``resolution.names.project_name``) that the resolver uses
for the ``project_name`` variable id, so the two modes cannot
disagree about what a legal name is.

The ``--var`` merge
-------------------
``--var`` values are passed to the resolver, but the resolver only
resolves variables the *blueprint declares* in its ``variables:[]``
list. Any ``--var`` key that is not declared is returned from
``resolve()`` unchanged — it never appears in ``resolved.value``.

That is correct for the resolver's *contract* (it resolves declared
variables), but it is the wrong outcome for the *plan builder*, which
consumes a plain variable mapping. A blueprint whose file
destinations reference a variable the blueprint does not declare
(e.g. a template-only ``{{ project }}`` in a ``destination``) would
otherwise fail with ``undefined variable: project`` even when the
user supplied it explicitly.

The fix is a merge step after the resolver runs, in
``_resolve_inputs``: any ``cli_vars`` key not already in
``resolved.value`` is folded into the combined mapping alongside
``project_name``. This makes the behaviour uniform for the create
path and the dry-run path (both consume ``inputs.variables``), so
the two cannot disagree about what the plan is built from — the
exact shape ADR-0018 requires.

The merge is *append-only*: an existing resolver-produced value is
never overwritten by a ``--var`` value. That preserves the
resolver's precedence rules (defaults < prompts < flags) for
*declared* variables; a ``--var`` for a declared variable is already
handled correctly by the resolver itself and does not need the
merge step. The merge exists only for variables that fell through
the resolver entirely.

Why dry-run collisions exit 7, not 6
------------------------------------
The write path (``_generate``) exits **6** when a plan collides with
existing files and ``--overwrite`` was not passed. The dry-run path
(``_run_dry_run``) exits **7** for the same underlying finding. The
distinction is not arbitrary — the two paths mean different things to
the caller, and the exit code is how CI distinguishes them:

* **6** — *the write was refused.* A real create attempt was made,
  the safety layer rejected the collision, and no files were
  modified. The user's next step is to re-run with
  ``--overwrite`` (which will succeed, with backups) or choose a
  different destination. A CI script that sees 6 knows "the create
  did not happen; retry may help."

* **7** — *the plan was produced and contains collisions.* Nothing
  was written. The plan is on stdout (JSON mode: a full
  ``GenerationPlan``; human mode: a rendered report). The user's
  next step is to *inspect* the plan, then decide whether to pass
  ``--overwrite``, change the destination, or edit the blueprint.
  A CI script that sees 7 knows "the plan is ready to review; do
  not re-run blindly."

If both outcomes returned 6, a CI script gating on "did the plan
collide?" could not distinguish a dry-run that succeeded from a
create that failed — and a "retry with `--overwrite` on 6" branch
would *write files* on what it thought was a read-only inspection.
That is exactly the safety failure dry-run exists to prevent, and
the distinct exit code is what makes the distinction enforceable.

``DRY_RUN_COLLISIONS`` (7) is defined in
``forge.core.exceptions.ExitCode`` alongside ``COLLISION`` (6). It
has no associated ``ForgeError`` subclass, because a dry-run with
collisions is not a failure — it is a *successful report*. The exit
code is raised via ``SystemExit`` directly, bypassing
``handle_exception`` (which classifies *exceptions*, and there is no
exception here). See ``docs/reference/DRY_RUN_PLAN.md``'s exit-code
table for the user-facing contract.

Why no ``try`` / ``except`` around the command body
---------------------------------------------------
Every exception this command raises is a ``ForgeError`` subclass
(user-facing) or is allowed to propagate to ``handle_exception``
(unexpected). The handler is the *single* renderer — printing here
would duplicate the handler's logic (stream, styling, hint) and
catching here would hide the exception from tests that assert on it
directly. The command has no side effects before phase 5, so there is
nothing to clean up on failure. The absence of a ``try`` block is
intentional and is the correct contract.

Why ``_stage_one`` has a ``# type: ignore[no-any-unimported]``
--------------------------------------------------------------
``TemplateRenderer`` is imported from ``forge.rendering``, whose
façade lazy-loads the concrete class from ``forge.rendering.renderer``,
which in turn imports ``jinja2`` — a package gated behind
``ignore_missing_imports`` in ``pyproject.toml``. Mypy can therefore
see the *name* ``TemplateRenderer`` but cannot resolve its shape, and
treats a parameter annotated with it as ``Any``. Strict mode's
``[no-any-unimported]`` check flags that.

The suppression is narrowly scoped to the ``def`` line and only
covers ``[no-any-unimported]``. It does not weaken the parameter's
*runtime* contract (``renderer`` must be a ``TemplateRenderer``;
passing anything else fails at ``.render(...)`` with a clear
``AttributeError``), the return type, or any other argument or
function. If ``jinja2`` ever gains type stubs (or the project drops
``ignore_missing_imports`` for it), the suppression becomes unused
and mypy's ``unused-ignore`` check — on under strict mode — will
force its removal in the same PR that lands the stubs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import typer

from forge.cli import prompts, ui
from forge.cli.errors import ExitCode
from forge.cli.registry import get_blueprint_document
from forge.conditions import ConditionalEvaluator
from forge.core.exceptions import (
    CollisionError,
    FilesystemConflictError,
    MissingRequiredFlagsError,
    SafetyError,
    TemplateRenderError,
    UserInputError,
)
from forge.dryrun import DryRunError, DryRunService, render_human, render_json
from forge.loading.result import Err
from forge.models import Blueprint, VariableDefinition
from forge.planning import FileMapper, PlannedFile
from forge.rendering import TemplateRenderer
from forge.resolution import ResolvedValues, VariableResolver
from forge.resolution.names import NAMED_VALIDATORS
from forge.safety import (
    StagingArea,
    backup_existing,
    detect_collisions,
    safe_resolve,
    validate_destination,
)

if TYPE_CHECKING:
    from forge.loading.errors import ValidationError
    from forge.rendering.errors import TemplateRenderError as RenderingTemplateError
    from forge.safety.errors import Finding as SafetyFinding

# -----------------------------------------------------------------------------
# Command module contract
# -----------------------------------------------------------------------------
app = typer.Typer(
    name="create",
    help="Create a project from a blueprint.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.callback()
def create(
    blueprint: str = typer.Argument(
        ...,
        metavar="BLUEPRINT",
        help=(
            "Blueprint ID (e.g., 'python.fastapi.api'). Use 'forge list' "
            "to see available blueprints."
        ),
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help=(
            "Project name. Must be lowercase, hyphenated, and a valid "
            "Python package name (e.g., 'payments-api'). Prompted for "
            "in interactive mode if omitted; required in non-interactive "
            "mode."
        ),
    ),
    output: Path = typer.Option(
        Path("."),
        "--output",
        "-o",
        help=(
            "Destination directory. Defaults to the current directory. "
            "The project will be created in '<output>/<name>/'."
        ),
        file_okay=False,
        dir_okay=True,
        writable=True,
        resolve_path=True,
    ),
    var: list[str] = typer.Option(
        [],
        "--var",
        "-v",
        help=(
            "Set a blueprint variable: '--var key=value'. Repeatable. "
            "Overrides the blueprint default and, in interactive mode, "
            "suppresses the prompt for that variable."
        ),
    ),
    no_interactive: bool = typer.Option(
        False,
        "--no-interactive",
        help=(
            "Disable interactive prompts. All required values must be "
            "provided via flags. Useful for CI and scripting."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Show the generation plan without writing any files. "
            "Leaves the filesystem unchanged."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "With --dry-run, render the plan as JSON. Ignored otherwise. "
            "The JSON shape is a versioned contract (see "
            "docs/reference/DRY_RUN_PLAN.md)."
        ),
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help=(
            "Allow overwriting existing files. Without this flag, the "
            "command fails if any target file already exists. When "
            "enabled, every replaced file is copied to a timestamped "
            "backup before any write."
        ),
    ),
) -> None:
    """Create a new project from a blueprint.

    Exit code contract:

    - 0 — Project created successfully (or dry-run completed, no
      collisions).
    - 1 — User error (missing required variable, invalid input,
      cancellation, planning collision, broken condition).
    - 2 — Blueprint error (not found, invalid, incompatible).
    - 3 — Filesystem safety refusal (traversal, absolute path, symlink
      escape, unwritable parent). ``--overwrite`` cannot help.
    - 4 — Template error (a template failed to render).
    - 5 — Internal error (unexpected exception).
    - 6 — Collision (a destination already exists and ``--overwrite``
      was not passed). Retry with ``--overwrite``, or edit the plan.
    - 7 — Dry-run found collisions. Nothing was written. Inspect the
      plan; then re-run with ``--overwrite`` if appropriate, or
      choose a different destination, or edit the blueprint.
    """
    inputs = _resolve_inputs(
        blueprint_id=blueprint,
        cli_name=name,
        cli_vars=_parse_var_flags(var),
        output=output,
        no_interactive=no_interactive,
    )

    if dry_run:
        _run_dry_run(inputs=inputs, overwrite=overwrite, json_output=json_output)
        return

    _generate(inputs=inputs, overwrite=overwrite)


# -----------------------------------------------------------------------------
# Flag parsing
# -----------------------------------------------------------------------------
def _parse_var_flags(var_flags: list[str]) -> dict[str, str]:
    """Parse repeated ``--var key=value`` flags into a mapping.

    Strict by design: the accepted syntax is exactly ``key=value`` with
    a non-empty key. Anything else is a usage error the user must fix
    before the command can do useful work, so it becomes a
    ``UserInputError`` (exit code 1) rather than a silent ignore.

    No type coercion here — the value is always a string at this layer.
    Coercion to boolean/enum/int is the resolver's job
    (``resolution.coercion``); duplicating it would create the second
    source of truth this design eliminates.
    """
    parsed: dict[str, str] = {}
    for raw in var_flags:
        if "=" not in raw:
            raise UserInputError(
                f"--var expects 'key=value', got {raw!r}. " "Example: --var author=alice",
                hint="Use --var once per value, e.g. --var a=1 --var b=2.",
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise UserInputError(
                f"--var expects a non-empty key before '=', got {raw!r}.",
                hint="Provide a variable id, e.g. --var author=alice.",
            )
        if key in parsed:
            raise UserInputError(
                f"--var key {key!r} was provided more than once.",
                hint="Provide each variable at most once.",
            )
        parsed[key] = value
    return parsed


# -----------------------------------------------------------------------------
# Input resolution
# -----------------------------------------------------------------------------
def _resolve_inputs(
    *,
    blueprint_id: str,
    cli_name: str | None,
    cli_vars: dict[str, str],
    output: Path,
    no_interactive: bool,
) -> _ResolvedInputs:
    """Collect user input, resolve variables, and evaluate conditions.

    Returns an immutable ``_ResolvedInputs`` bundle. Both the ``create``
    path and the ``--dry-run`` path consume this exact bundle, so the
    two paths cannot disagree about what the plan is built from.
    """
    blueprint = get_blueprint_document(blueprint_id)
    interactive = prompts.is_interactive() and not no_interactive

    # Phase 2a: project name (command argument, not blueprint variable).
    project_name = _resolve_project_name(cli_name=cli_name, interactive=interactive)

    # Phase 2b: blueprint variables (resolver).
    prompt_values = (
        _collect_interactive_answers(blueprint, already_supplied=cli_vars) if interactive else {}
    )
    resolved = VariableResolver().resolve(
        blueprint,
        cli_args=cli_vars,
        prompt_answers=prompt_values,
    )
    if isinstance(resolved, Err):
        raise _user_error_from_resolver(resolved.error, source=f"blueprint:{blueprint_id}")

    # Phase 3: conditions against resolved variables.
    evaluated = ConditionalEvaluator().evaluate(
        blueprint,
        resolved.value.as_dict(),
    )
    if isinstance(evaluated, Err):
        raise _user_error_from_conditions(
            evaluated.error,
            source=f"blueprint:{blueprint_id}",
        )

    # Compose the final variable set.
    #
    # Three sources, merged with a deliberate precedence order:
    #
    #   1. ``resolved.value`` — the resolver's output. This is the
    #      authoritative value for every *declared* variable; the
    #      resolver has already applied its own precedence (CLI flag
    #      > prompt answer > blueprint default) and validated types,
    #      patterns, enums, and named rules. Nothing in the merge
    #      below may overwrite a value from this dict.
    #
    #   2. ``cli_vars`` for keys the resolver did *not* produce. A
    #      ``--var`` for a variable the blueprint does not declare
    #      falls through the resolver untouched — its contract is
    #      "resolve declared variables" — but is still a legitimate
    #      input for the plan builder. Blueprints reference template
    #      variables (e.g. ``{{ project }}`` in a ``destination``)
    #      that they do not also declare in ``variables:[]``, and
    #      ``--var`` is the documented way to supply those. See the
    #      module docstring's "The ``--var`` merge" section.
    #
    #   3. ``project_name`` — the command's own argument, folded into
    #      the mapping because blueprint templates may reference it
    #      by name. ``--name`` wins over any ``--var project_name=…``
    #      by convention (the explicit flag is the canonical source);
    #      a ``--var project_name`` collision is a user mistake the
    #      validator catches at the resolver boundary if the blueprint
    #      declares ``project_name``.
    combined = dict(resolved.value.as_dict())

    for key, value in cli_vars.items():
        if key not in combined:
            combined[key] = value

    combined["project_name"] = project_name

    return _ResolvedInputs(
        blueprint=blueprint,
        project_name=project_name,
        destination=output / project_name,
        variables=ResolvedValues(MappingProxyType(combined)),
        surviving_conditions=evaluated.value,
    )


def _resolve_project_name(*, cli_name: str | None, interactive: bool) -> str:
    """Return a validated project name, prompting or failing as needed."""
    if interactive:
        return cli_name or prompts.ask_text(
            "Project name",
            validator=_prompt_validator_for("project_name"),
        )

    if cli_name is None:
        raise MissingRequiredFlagsError(command="create", missing=("name",))
    problem = NAMED_VALIDATORS["project_name"](cli_name)
    if problem is not None:
        message, hint = problem
        raise UserInputError(message, hint=hint)
    return cli_name


def _collect_interactive_answers(
    blueprint: Blueprint,
    *,
    already_supplied: dict[str, str],
) -> dict[str, object]:
    """Prompt for every blueprint variable not already supplied by a flag."""
    answers: dict[str, object] = {}
    for variable in blueprint.variables:
        if variable.id in already_supplied:
            continue  # flags win; do not re-ask
        answers[variable.id] = _prompt_for_variable(variable)
    return answers


def _prompt_for_variable(variable: VariableDefinition) -> object:
    """Prompt for one variable using its declared type and validator."""
    if variable.type.value == "boolean":
        return prompts.ask_confirm(
            f"{variable.id}?",
            default=bool(variable.default),
        )
    if variable.type.value == "enum":
        choices = (
            list(variable.validation.enum)
            if variable.validation and variable.validation.enum
            else []
        )
        return prompts.ask_select(variable.id, choices)
    return prompts.ask_text(
        variable.description or variable.id,
        default=str(variable.default) if variable.default is not None else None,
        validator=_prompt_validator(variable),
    )


def _prompt_validator(variable: VariableDefinition) -> prompts.Validator | None:
    """Adapt a blueprint variable's rules to the prompt engine's signature."""
    if variable.id not in NAMED_VALIDATORS:
        return None
    return _prompt_validator_for(variable.id)


def _prompt_validator_for(named: str) -> prompts.Validator:
    """Adapt a named validator from ``resolution.names`` for the prompt engine."""
    validator = NAMED_VALIDATORS[named]

    def _validate(value: str) -> str | None:
        problem = validator(value)
        return None if problem is None else problem[1]

    return _validate


# -----------------------------------------------------------------------------
# Dry-run
# -----------------------------------------------------------------------------
def _run_dry_run(
    *,
    inputs: _ResolvedInputs,
    overwrite: bool,
    json_output: bool,
) -> None:
    """Build and render a plan; never touch the destination.

    Delegates to ``DryRunService`` (ADR-0018) so the plan's composition,
    ordering, and JSON shape are the same as any other caller of the
    dry-run module. The command's job here is only: pick a renderer,
    emit, and map the outcome to an exit code.
    """
    result = DryRunService().plan(
        blueprint=inputs.blueprint,
        variables=inputs.variables.as_dict(),
        destination=inputs.destination,
        overwrite=overwrite,
    )
    if isinstance(result, DryRunError):
        # A dry-run failure is a planning failure surfaced as data —
        # route through the same UserInputError channel the create path
        # uses so the rendering, exit code, and hint discipline match.
        raise _user_error_from_dry_run(result)

    text = render_json(result) if json_output else render_human(result)
    ui.emit(text)

    # Collisions are a *successful dry-run* that found something. Exit
    # 7 (``DRY_RUN_COLLISIONS``), not 6 (``COLLISION``):
    #
    #   * 6 is the *create* path's code, meaning "a real write was
    #     refused because the destination exists." The write did not
    #     happen. The user's next step is to pass ``--overwrite`` or
    #     change the destination, then re-run create.
    #
    #   * 7 is the *dry-run* code, meaning "the plan was produced
    #     successfully and is on stdout; it contains collisions."
    #     Nothing was written. The user's next step is to *inspect*
    #     the plan and then decide — pass ``--overwrite`` on a
    #     subsequent create, or change the destination, or edit the
    #     blueprint.
    #
    # A CI script that branches on 7 can parse the plan and take
    # action without re-running the dry-run. A script that saw 6 here
    # would not know whether a write had been attempted (and possibly
    # partially completed) — an ambiguity the distinct code
    # eliminates. See docs/reference/DRY_RUN_PLAN.md's exit-code
    # table and the module docstring's "Why dry-run collisions exit
    # 7, not 6" section.
    if result.has_collisions:
        raise SystemExit(ExitCode.DRY_RUN_COLLISIONS)


# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------
def _generate(
    *,
    inputs: _ResolvedInputs,
    overwrite: bool,
) -> None:
    """Run phases 4 to 7: plan, validate, back up, stage, commit.

    See the module docstring for ordering rationale. Phase 5 is the
    safety gate; phase 6 is the backup gate; phase 7 is the only phase
    that writes to the destination.
    """
    # Phase 4: plan (pure).
    plan_result = FileMapper().map(
        inputs.blueprint,
        inputs.variables.as_dict(),
        conditional_groups=inputs.surviving_conditions,
    )
    if isinstance(plan_result, Err):
        raise _user_error_from_mapping(plan_result.error, source=f"blueprint:{inputs.blueprint.id}")
    plan = plan_result.value

    # Phase 5: safety validation.
    findings = _collect_safety_findings(
        plan=plan,
        destination=inputs.destination,
        overwrite=overwrite,
    )
    if findings:
        _raise_safety(findings, operation="create")

    # Phase 6: backups (only when --overwrite and only for files that
    # already exist).
    if overwrite:
        existing = _existing_targets(plan, inputs.destination)
        if existing:
            try:
                backup_existing(existing)
            except OSError as exc:
                raise FilesystemConflictError(
                    f"Could not back up existing files before overwrite: {exc}",
                    hint=(
                        "Check permissions and free disk space, then retry. "
                        "No files have been modified."
                    ),
                ) from exc

    # Phase 7: staging and commit.
    blueprint_root = _blueprint_root(inputs.blueprint)
    renderer = TemplateRenderer()

    # ``mkdtemp`` (used by ``StagingArea.for_root``) requires its parent
    # to exist. Creating the root is idempotent and side-effect-free if
    # it already exists.
    inputs.destination.mkdir(parents=True, exist_ok=True)

    with StagingArea.for_root(inputs.destination) as stage:
        for planned in plan:
            _stage_one(
                stage=stage,
                renderer=renderer,
                planned=planned,
                blueprint_root=blueprint_root,
                variables=inputs.variables.as_dict(),
            )
        committed = stage.commit()

    ui.success(
        f"Created {inputs.project_name!r} from {inputs.blueprint.id} "
        f"({len(committed)} file(s))."
    )


def _collect_safety_findings(
    *,
    plan: list[PlannedFile],
    destination: Path,
    overwrite: bool,
) -> list[SafetyFinding]:
    """Return every safety finding for the plan against the destination.

    Two passes, aggregated into one report:

    * Per-file structural validation — ``safe_resolve`` +
      ``validate_destination``.
    * Plan-vs-disk collisions — ``detect_collisions`` with the
      ``--overwrite`` consent threaded through.
    """
    findings: list[SafetyFinding] = []
    for planned in plan:
        resolved, finding = safe_resolve(planned.destination, destination)
        if finding is not None:
            findings.append(finding)
            continue
        # mypy cannot infer the (Path, None) / (None, Finding) union
        # narrowing; a runtime check doubles as the narrowing hint.
        if resolved is None:  # pragma: no cover - defensive
            raise RuntimeError(
                "safe_resolve returned (None, None) — a contract violation "
                f"for destination {planned.destination!r}. This is a bug "
                "in forge.safety; please report it."
            )
        findings.extend(validate_destination(resolved, overwrite=overwrite))
    findings.extend(detect_collisions(plan, destination, overwrite=overwrite))
    return findings


def _raise_safety(findings: list[SafetyFinding], *, operation: str) -> None:
    """Translate safety findings into the CLI error hierarchy.

    The safety layer's ``Finding`` carries ``(code, path, message,
    hint, root)``. The CLI's ``SafetyError`` / ``CollisionError``
    accept a four-tuple projection ``(code, path, message, hint)``.
    This function projects and raises the correct *type*:

    * Collision codes → ``CollisionError`` (exit 6), which the handler
      renders with an ``--overwrite`` hint.
    * Anything else → ``SafetyError`` (exit 3), which the handler
      renders with a "fix the plan" hint.

    Choosing the type here (not in the handler) keeps the exit-code
    contract (ADR-0013/ADR-0017) visible at the raise site — a
    reviewer reading ``_raise_safety`` sees the two outcomes
    side-by-side.
    """
    projected = tuple((f.code, f.path, f.message, f.hint) for f in findings)
    collision_codes = {"collision.exists", "plan.case_insensitive_duplicate"}
    is_collision = any(f.code in collision_codes for f in findings)
    if is_collision:
        raise CollisionError(findings=projected, operation=operation)
    raise SafetyError(findings=projected, operation=operation)


# ``[no-any-unimported]`` is suppressed on the ``def`` line only. See
# the module docstring's "Why ``_stage_one`` has a
# ``# type: ignore[no-any-unimported]``" section for the reasoning.
# The suppression is load-bearing for the *type checker* (which cannot
# follow ``TemplateRenderer`` through the ``forge.rendering`` façade
# into the ``jinja2``-gated module) but does not affect runtime
# behaviour: the parameter is a ``TemplateRenderer`` at call time, and
# passing anything else fails at ``.render(...)`` with a clear
# ``AttributeError``.
def _stage_one(  # type: ignore[no-any-unimported]
    *,
    stage: StagingArea,
    renderer: TemplateRenderer,
    planned: PlannedFile,
    blueprint_root: Path,
    variables: dict[str, Any],
) -> None:
    """Render or copy one planned file into the staging area.

    * ``COPY`` — read bytes verbatim and write them to staging.
    * ``RENDER`` — render through the sandboxed renderer; translate
      any ``TemplateRenderError`` into the CLI error hierarchy.
    """
    source_path = blueprint_root / planned.source

    if planned.is_render:
        result = renderer.render(source_path, variables)
        if not result.is_ok():
            raise _as_template_error(result.error, source=str(source_path))
        stage.write_text(planned.destination, result.value)
        return

    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise FilesystemConflictError(
            f"Could not read source file {planned.source!r} for copy: {exc}",
            hint="Check the blueprint's file entry; the source must be readable.",
        ) from exc
    stage.write_bytes(planned.destination, data)


def _existing_targets(plan: list[PlannedFile], root: Path) -> list[Path]:
    """Return absolute paths of plan destinations that exist on disk.

    Uses ``lstat`` — a broken symlink is a name in use and will be
    replaced by ``--overwrite``, so it must be backed up. Fail closed:
    an unreadable entry is treated as existing, because a spurious
    backup is cheap and a missed backup is data loss.
    """
    existing: list[Path] = []
    for planned in plan:
        target = root / planned.destination
        try:
            os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError:
            pass  # fail closed: treat as existing
        existing.append(target)
    return existing


def _blueprint_root(blueprint: Blueprint) -> Path:
    """Return the directory the blueprint's templates are relative to.

    Uses the discovery service (the single source of truth for where a
    blueprint lives) rather than threading a path through the model.
    The ``Blueprint`` is a pure document (ADR-0007/0008); adding a
    filesystem path to it would couple the schema to the loader's
    layout.

    A ``DiscoveryError`` here means the blueprint vanished between
    phase 1 and phase 7 — a filesystem race at the CLI boundary,
    rendered with exit code 3.
    """
    from forge.discovery import BlueprintDiscoveryService

    located = BlueprintDiscoveryService().load_with_path(blueprint.id)
    if located is None:  # pragma: no cover - defensive
        raise FilesystemConflictError(
            f"Blueprint {blueprint.id!r} disappeared between load and generate.",
            hint=(
                "This is unexpected. Re-run the command; if it persists, "
                "check whether the blueprint file was moved or deleted."
            ),
        )
    path, _document = located
    return path.parent


# -----------------------------------------------------------------------------
# Error adapters (domain → CLI hierarchy)
# -----------------------------------------------------------------------------
#
# These translate the *data*-shaped errors produced by the loading,
# resolution, conditions, planning, dry-run, and rendering modules into
# the *type*-shaped errors the CLI handler classifies. Keeping them
# here (not in the handler) means:
#
#   * the CLI handler has one job (render), and
#   * each error adapter is next to the code that raised it, so a
#     future change to a module's error shape is caught at the same
#     place it was introduced.
#
def _user_error_from_resolver(
    errors: list[ValidationError],
    *,
    source: str,
) -> UserInputError:
    """Adapt the resolver's ``ValidationError`` list into ``UserInputError``.

    ``errors`` is typed ``list[ValidationError]`` (imported under
    ``TYPE_CHECKING``) rather than the previous bare ``list`` — mypy
    strict flags the unparameterised form as ``[type-arg]`` because a
    bare ``list`` is ``list[Any]`` and cannot prove that ``e.format()``
    exists on every element. The concrete type gives mypy enough
    information to verify the call, and gives a reader the shape of
    the list without having to look up the resolver's return type.
    """
    if not errors:
        return UserInputError(
            f"Input validation failed for {source} (no details).",
        )
    joined = "\n  ".join(e.format() for e in errors)
    return UserInputError(
        f"Input validation failed for {source}:\n  {joined}",
        hint="Fix the highlighted inputs; each line names the field and the fix.",
    )


def _user_error_from_conditions(error: Any, *, source: str) -> UserInputError:
    findings = error.findings
    if not findings:
        return UserInputError(
            f"Condition evaluation failed for {source} (no details).",
        )
    lines = [f"{f.condition}: {f.message} — hint: {f.hint}" for f in findings]
    return UserInputError(
        f"Condition errors for {source}:\n  " + "\n  ".join(lines),
        hint="Fix the conditional_files entries named above.",
    )


def _user_error_from_mapping(error: Any, *, source: str) -> UserInputError:
    if not error.findings:
        return UserInputError(f"Planning failed for {source} (no details).")
    joined = "\n  ".join(f.format() for f in error.findings)
    return UserInputError(
        f"Planning failed for {source}:\n  {joined}",
        hint="Fix the file mappings or destinations named above.",
    )


def _user_error_from_dry_run(error: DryRunError) -> UserInputError:
    """Adapt a ``DryRunError`` into the CLI's user-error channel."""
    findings = error.findings
    if not findings:
        return UserInputError(
            f"Dry-run failed ({error.operation}); no details available.",
        )
    lines = [f"[{f.code}] {f.path}: {f.message} — hint: {f.hint}" for f in findings]
    return UserInputError(
        f"Dry-run failed ({error.operation}):\n  " + "\n  ".join(lines),
        hint="Fix the issues named above and re-run.",
    )


def _as_template_error(
    error: RenderingTemplateError,
    *,
    source: str,
) -> TemplateRenderError:
    """Translate a rendering-side error into the CLI's error type.

    The rendering-side error carries ``template``, ``line``, ``col``,
    and ``name`` — richer than the CLI's ``TemplateRenderError``
    (which is a ``StructuredError`` with ``(pointer, message)``
    pairs). This function projects them into a single tuple so the
    handler can render it uniformly.
    """
    pointer = ""
    if error.line is not None:
        pointer = f"line {error.line}"
        if error.col is not None:
            pointer += f", col {error.col}"
    return TemplateRenderError(
        source=source,
        errors=((pointer, error.message),),
    )


# -----------------------------------------------------------------------------
# Internal result type
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _ResolvedInputs:
    """Validated, immutable inputs consumed by both create and dry-run.

    ``variables`` is a ``ResolvedValues`` — immutable, resolver-owned —
    not a plain ``dict``. That is the KPI-enforcement mechanism from
    ADR-0011: downstream code that requires ``ResolvedValues`` in its
    signature cannot be handed unvalidated data.

    ``surviving_conditions`` carries the *evaluated* conditional groups
    so the planner does not re-evaluate them. Re-evaluation is cheap
    but the plan must be built from the *same* evaluation the create
    path used — otherwise a dry-run and a create could disagree under
    some future non-determinism in the evaluator.
    """

    blueprint: Blueprint
    project_name: str
    destination: Path
    variables: ResolvedValues
    surviving_conditions: list[Any]


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = ["app"]
