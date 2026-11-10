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
phase is a pure function (or a context manager) that either returns a
result or raises a typed error the CLI handler classifies.

    1. **Load** the blueprint document (``get_blueprint_document``).
       Fails with exit code 2 on malformed/missing blueprints.
    2. **Resolve** project name and variables (this module +
       ``VariableResolver``). Fails with exit code 1 on user input
       errors.
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
       any I/O error during backup, and rolls back partial backups
       so the filesystem is left exactly as it was at phase entry.
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
guarantee that the safety layer's ADR-0017 makes: *no write escapes
staging*.

Dual-mode input handling
------------------------
The command supports two input modes, dispatched once in
``_resolve_variables`` (see ADR-0006):

- **Non-interactive** — ``--no-interactive`` was passed, or stdin is
  not a TTY. All required values must come from flags. If any are
  missing, the command fails *before any side effects* with a single
  ``MissingRequiredFlagsError`` listing every gap. This is the
  contract scripted callers depend on: fail fast, fail complete,
  fail with code 1.

- **Interactive** — stdin is a TTY and ``--no-interactive`` was not
  passed. Any value not supplied as a flag is collected by the
  wizard in ``cli.prompts``. Flags win: a value supplied on the
  command line is *not* re-asked. Ctrl+C at any prompt raises
  ``PromptCancelledError``, which the CLI handler renders as a
  dimmed "Cancelled." line and exits with code 1 — no traceback.

Both modes converge on ``VariableResolver.resolve`` from
``forge.resolution`` (ADR-0011) for *blueprint variables*. The
project name is a *command argument*, not a blueprint variable, and
is therefore validated separately — but by the *same* named
validator the resolver uses for the ``project_name`` variable id,
so the two modes cannot disagree about what a legal name is.

Why the callback, and how ``forge create`` becomes a leaf command
----------------------------------------------------------------
This module exposes *one* command. Typer's default is to interpret
``@app.command()`` as a subcommand of a group, which makes the user
type ``forge create create <blueprint>`` — obviously wrong. The fix
is to put the command's function on the app as a **callback**:

    app = typer.Typer(name="create", help="…")

    @app.callback()
    def create(…): …

The registry (``forge.cli.registry``) detects this shape and registers
the callback's function directly on the root app as a leaf command
under the name in ``app.info.name``. That is what makes
``forge create python.fastapi.api --name my-api`` parse with
``python.fastapi.api`` as the callback's ``blueprint`` argument — not
as a subcommand.

Do **not** "simplify" this by converting ``@app.callback()`` to
``@app.command()`` and adding a second ``Typer`` layer, or by
re-adding ``invoke_without_command`` in the hope that it fixes the
nesting. It does not.

Why ``name`` is not marked required at the Typer layer
------------------------------------------------------
``typer.Option(..., ...)`` (Ellipsis) tells Typer the option is
mandatory, and Typer enforces it *before* the command function body
runs. That would break the interactive wizard: Typer would reject
``forge create python.fastapi.api`` (no ``--name``) with a Click
usage error *before* the wizard could prompt for the name.

The fix is to move the requirement from Typer to the domain:

- Typer sees ``name`` as optional (``default=None``).
- Non-interactive mode re-checks: missing ``--name`` becomes a
  ``MissingRequiredFlagsError`` with a hint, exit code 1.
- Interactive mode prompts for the name if it was not supplied.

The *contract* ("a project must have a name") is unchanged. Only the
*enforcement point* moves — from the parser, where it prevents the
wizard, to the domain, where it can coexist with the wizard.

Why the project name is validated *outside* the resolver
--------------------------------------------------------
The resolver's contract is "resolve the variables the *blueprint*
declares" (ADR-0011). ``project_name`` is not one of them — it is a
command argument that happens to share a name with a well-known
variable id. Threading it through the resolver would either require
every blueprint to declare a variable it does not semantically own,
or leave ``resolved["project_name"]`` as a ``KeyError`` for any
blueprint that does not.

So the project name is collected here and validated here, using the
*same* named validator (``resolution.names.project_name``) that the
resolver would apply if ``project_name`` were a blueprint variable.
One rule, two enforcement points — interactive prompt (real-time
UX) and non-interactive check (scripted correctness) — both backed
by the same function.

Why the command calls ``get_blueprint_document``, not ``get_blueprint``
---------------------------------------------------------------------
The registry exposes *two* blueprint accessors because two callers
have different needs:

* ``get_blueprint(blueprint_id) -> BlueprintEntry`` — a catalogue
  view (``id``, ``display_name``, raw ``variables`` tuple).
  Sufficient for ``forge list`` and for the wizard's *selection*
  menu; **not** sufficient to resolve values against.

* ``get_blueprint_document(blueprint_id) -> Blueprint`` — the
  loaded, schema-validated, model-validated document. That is what
  any caller that *does something with* the blueprint needs.

``create`` needs the second. Widening ``BlueprintEntry`` to carry a
``path`` would drag the loader's types into the catalogue view — the
exact coupling the two-accessor design exists to prevent.

Conditional file inclusion
--------------------------
Conditional file groups are evaluated *after* variable resolution and
*before* file planning, so the planner only sees the groups whose
conditions hold (ADR-0016).

    variables ──► ConditionalEvaluator.evaluate ──► surviving groups
                                                            │
                                                            ▼
    blueprint ──► FileMapper.map (unconditional + surviving)

The evaluator is a pure function; it never touches the filesystem.
Its failures (undefined variable in a condition, invalid condition
syntax) are aggregated into a single ``ConditionError`` and routed
through the same ``UserInputError`` channel the resolver and planner
use, so a broken condition surfaces as a user error (exit 1) with
actionable hints — exactly like a broken variable.

Why no ``try`` / ``except`` around the command body
---------------------------------------------------
Every exception this command raises is a ``ForgeError`` subclass
(user-facing) or is allowed to propagate to ``handle_exception``
(unexpected). The handler is the *single* renderer — printing here
would duplicate the handler's logic (stream, styling, hint) and
catching here would hide the exception from tests that assert on it
directly. The command has no side effects before validation
completes, so there is nothing to clean up on failure. The absence
of a ``try`` block is intentional and is the correct contract.

Variable resolution — the current pipeline
-----------------------------------------
    blueprint  ──┐
    cli_vars   ──┼──►  VariableResolver.resolve  ──►  ResolvedValues
    prompts    ──┘                                    (immutable,
                                                       validated,
                                                       typed)

``VariableResolver.resolve`` applies the full precedence chain
(CLI flags > prompt answers > blueprint defaults) and validates each
value against its ``VariableDefinition`` (type coercion, pattern,
enum, named validators). Its output type ``ResolvedValues`` is
immutable and resolver-owned, so downstream code can require it in
their signatures and be confident that unvalidated input never
reaches them — the KPI this command exists to deliver.

Safety is a *gate*, not a *phase of planning*
---------------------------------------------
The plan is pure — it describes what *would* be written. Safety is
the gate that asks "is it safe to write *here*?" The two are
deliberately separated:

* ``FileMapper`` (phase 4) answers "does the plan make sense?" —
  collisions *within* the plan, unsafe *destinations* from variable
  substitution. It is pure and has no filesystem access.

* ``forge.safety`` (phase 5) answers "is it safe to write *to this
  filesystem*?" — does the destination exist? is a symlink in the
  way? is the parent writable? It *does* touch the filesystem, but
  only to *read* (``lstat``, ``os.access``); no writes happen until
  phase 7.

Collapsing them would either make planning impure (breaking its
testability — you cannot unit-test a plan that needs a filesystem)
or make safety inconsistent (the same traversal check reimplemented
inside the planner — the divergence ADR-0015 explicitly forbids).

Why two safety error types (``SafetyError`` vs ``CollisionError``)
------------------------------------------------------------------
Structural safety failures (traversal, absolute path, symlink escape,
unwritable parent) cannot be fixed by ``--overwrite`` — the flag
consents to replacing a file, not to writing outside the root. They
surface as ``SafetyError`` with exit code 3.

Collisions *can* be fixed by ``--overwrite`` (with backups). They
surface as ``CollisionError`` with exit code 6. The distinction is
load-bearing: CI scripts branch on the exit code to decide whether
to retry with ``--overwrite`` or fail for human review. Merging the
two would force every caller to inspect a message to make that
decision.

Why the pre-flight conflict check is not duplicated
---------------------------------------------------
``forge.safety.detect_collisions`` already reports existing files.
The pre-flight check in this command does *not* repeat it — the
authoritative check is in phase 5. A pre-flight here would be a
second source of truth for the same question, and the two would
drift (the pre-flight checking ``destination.exists()`` on the
*directory* while the safety layer checks individual *files* is a
concrete example of drift that produces confusing errors).

The command's ``--overwrite`` flag is threaded into the safety
layer's collision detection, so the layer can decide what to do
with its own findings. The command never inspects findings
directly; it only translates ``CollisionError``/``SafetyError``
into the exit code (which the CLI handler then renders).

Why the safety errors are translated, not re-raised verbatim
------------------------------------------------------------
``forge.safety.errors.SafetyError`` and ``CollisionError`` are
*implementation* types — they carry ``Finding`` objects with
internal detail (``root``, raw paths, etc.). The CLI contract is
``forge.core.exceptions.SafetyError`` / ``CollisionError``, whose
findings shape is stable and CLI-appropriate. The translation is a
lossy projection (four fields per finding, not five) that:

* keeps ``forge.safety`` free of any dependency on ``forge.cli`` or
  ``forge.core`` — it is a primitive; primitives do not depend on
  their consumers;
* puts the exit code where ADR-0005 says it belongs: in
  ``core.exceptions``, not in the safety layer;
* gives the CLI handler a ``ForgeError`` subclass to dispatch on,
  consistent with every other error category.

Why the ``NotImplementedError`` is gone
---------------------------------------
An earlier revision of this command ended the non-dry path with
``NotImplementedError`` because the writer had not yet landed. The
writer is now the safety layer + staging area (tasks 1.3.4-1.3.7).
The command therefore runs the full pipeline end to end. If a future
writer (say, a "commit hooks" phase) is not yet ready, it must go
through the same pattern: build the plan, run the safety checks,
raise a typed error for the missing phase — never a silent no-op.

Why ``_stage_one`` has a ``# type: ignore[no-any-unimported]``
--------------------------------------------------------------
``TemplateRenderer`` is imported from ``forge.rendering``, whose
façade lazy-imports the concrete class from ``forge.rendering.renderer``,
which in turn imports ``jinja2`` — a package that is gated behind
``ignore_missing_imports`` in ``pyproject.toml``. Mypy can therefore
see the *name* ``TemplateRenderer`` but cannot resolve the class's
shape, and treats a parameter annotated with it as ``Any``. Strict
mode's ``[no-any-unimported]`` check flags that.

The suppression is narrowly scoped to the ``def`` line and only
covers ``[no-any-unimported]``. It does not weaken:

    * the parameter's *runtime* contract (``renderer`` must be a
      ``TemplateRenderer``; passing anything else fails at
      ``.render(...)`` with a clear ``AttributeError``),
    * the return type (``None``), which mypy still checks,
    * any other argument or any other function in this module.

If ``jinja2`` ever gains type stubs (or the project drops
``ignore_missing_imports`` for it), the suppression becomes unused
and mypy's ``unused-ignore`` check — on under strict mode — will
force its removal in the same PR that lands the stubs. That is the
desired signal: the ignore is a *placeholder for an external
dependency's gap*, not a permanent part of this file's contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import typer

from forge.cli import prompts, ui
from forge.cli.registry import get_blueprint_document
from forge.conditions import ConditionalEvaluator
from forge.conditions.errors import ConditionError
from forge.core.exceptions import (
    CollisionError as CoreCollisionError,
)
from forge.core.exceptions import (
    FilesystemConflictError,
    MissingRequiredFlagsError,
    TemplateRenderError,
    UserInputError,
)
from forge.core.exceptions import (
    SafetyError as CoreSafetyError,
)
from forge.loading.errors import ValidationError
from forge.loading.result import Err
from forge.models import Blueprint, VariableDefinition
from forge.planning import FileMapper, MappingError, PlannedFile
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
    from forge.conditions.evaluator import SurvivingGroup
    from forge.rendering.errors import TemplateRenderError as _RenderError
    from forge.safety.errors import Finding as _SafetyFinding

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
            "Python package name (e.g., 'payments-api'). Prompted for in "
            "interactive mode if omitted; required in non-interactive mode."
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

    - 0 — Project created successfully (or dry-run completed).
    - 1 — User error (missing required variable, invalid input,
      cancellation, planning collision, broken condition).
    - 2 — Blueprint error (not found, invalid, incompatible).
    - 3 — Filesystem safety refusal (traversal, absolute path, symlink
      escape, unwritable parent). ``--overwrite`` cannot help.
    - 4 — Template error (a template failed to render).
    - 5 — Internal error (unexpected exception).
    - 6 — Collision (a destination already exists and ``--overwrite``
      was not passed). Retry with ``--overwrite``, or edit the plan.
    """
    blueprint_document, inputs, surviving_groups = _resolve_variables(
        blueprint_id=blueprint,
        cli_name=name,
        cli_vars=_parse_var_flags(var),
        output=output,
        no_interactive=no_interactive,
    )
    _generate(
        blueprint=blueprint_document,
        inputs=inputs,
        conditional_groups=surviving_groups,
        dry_run=dry_run,
        overwrite=overwrite,
    )


# -----------------------------------------------------------------------------
# Flag parsing
# -----------------------------------------------------------------------------
def _parse_var_flags(var_flags: list[str]) -> dict[str, str]:
    """Parse repeated ``--var key=value`` flags into a mapping.

    Kept deliberately simple and strict: the accepted syntax is exactly
    ``key=value`` with a non-empty key. Anything else is a usage error
    the user needs to fix before the command can do useful work, so we
    surface it as a ``UserInputError`` rather than silently ignoring
    the flag.

    Why no type coercion here: the *value* is always a string at this
    layer. Coercion to boolean/enum/etc. is the resolver's job
    (``resolution.coercion``), and duplicating it here would create the
    second source of truth this design exists to eliminate.
    """
    parsed: dict[str, str] = {}
    for raw in var_flags:
        if "=" not in raw:
            raise UserInputError(
                f"--var expects 'key=value', got {raw!r}. " f"Example: --var author=alice"
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise UserInputError(f"--var expects a non-empty key before '=', got {raw!r}.")
        if key in parsed:
            raise UserInputError(
                f"--var key {key!r} was provided more than once. "
                f"Provide each variable at most once."
            )
        parsed[key] = value
    return parsed


# -----------------------------------------------------------------------------
# Variable resolution (unchanged from previous revision)
# -----------------------------------------------------------------------------
def _resolve_variables(
    *,
    blueprint_id: str,
    cli_name: str | None,
    cli_vars: dict[str, str],
    output: Path,
    no_interactive: bool,
) -> tuple[Blueprint, _ResolvedInputs, list[SurvivingGroup]]:
    """Collect user input, resolve variables, evaluate conditional groups.

    See the module docstring's "Dual-mode input handling" and "Why the
    project name is validated *outside* the resolver" sections for the
    design rationale. This function is the "collect and validate"
    half of the command; ``_generate`` is the "plan and commit" half.

    Returns:
        ``(blueprint, inputs, surviving_groups)`` — everything
        ``_generate`` needs, with no re-fetching and no re-validation.
    """
    blueprint = get_blueprint_document(blueprint_id)

    interactive = prompts.is_interactive() and not no_interactive

    # Phase 2a: project name (command argument, not blueprint variable).
    if not interactive:
        _require_flags(command="create", values={"--name": cli_name})
        assert cli_name is not None  # noqa: S101
        project_name = cli_name
        _validate_project_name(project_name)
    else:
        project_name = cli_name or prompts.ask_text(
            "Project name",
            validator=_prompt_validator_for("project_name"),
        )

    # Phase 2b: blueprint variables (resolver).
    prompt_values = (
        _collect_interactive_answers(blueprint, already_supplied=cli_vars) if interactive else {}
    )
    resolver = VariableResolver()
    result = resolver.resolve(
        blueprint,
        cli_args=cli_vars,
        prompt_answers=prompt_values,
    )
    if isinstance(result, Err):
        raise _as_user_input_error(result.error, source=f"blueprint:{blueprint_id}")

    # Phase 3: conditional groups (against resolved variables).
    evaluation = ConditionalEvaluator().evaluate(blueprint, result.value.as_dict())
    if isinstance(evaluation, Err):
        raise _as_user_input_error_from_conditions(
            evaluation.error, source=f"blueprint:{blueprint_id}"
        )
    surviving_groups = evaluation.value

    # Phase 4 (partial): combine project_name into the resolved set.
    # The planner will see this combined set — the project name is
    # available for destination substitution even though it is not a
    # blueprint variable, because the planner consumes *what it needs*
    # from the mapping without caring which source it came from.
    combined = dict(result.value.as_dict())
    combined["project_name"] = project_name

    inputs = _ResolvedInputs(
        blueprint_id=blueprint.id,
        project_name=project_name,
        destination=output / project_name,
        variables=ResolvedValues(MappingProxyType(combined)),
    )
    return blueprint, inputs, surviving_groups


def _validate_project_name(value: str) -> None:
    """Reject ``value`` with ``UserInputError`` if it fails the shared rule."""
    problem = NAMED_VALIDATORS["project_name"](value)
    if problem is None:
        return
    message, hint = problem
    raise UserInputError(f"{message} Hint: {hint}")


def _require_flags(*, command: str, values: dict[str, str | None]) -> None:
    """Raise ``MissingRequiredFlagsError`` if any value is ``None``."""
    missing = [flag for flag, value in values.items() if value is None]
    if missing:
        raise MissingRequiredFlagsError(
            command=command,
            missing=tuple(flag.lstrip("-").replace("-", "_") for flag in missing),
        )


def _as_user_input_error(errors: list[ValidationError], *, source: str) -> UserInputError:
    """Convert resolver errors into one ``UserInputError`` for the handler."""
    if not errors:
        return UserInputError(f"Input validation failed for {source} (no details available).")
    joined = "\n  ".join(err.format() for err in errors)
    return UserInputError(f"Input validation failed for {source}:\n  {joined}")


def _as_user_input_error_from_conditions(error: ConditionError, *, source: str) -> UserInputError:
    """Convert a ``ConditionError`` into a ``UserInputError``."""
    findings = error.findings
    if not findings:
        return UserInputError(f"Condition evaluation failed for {source} (no details available).")
    lines = [f"{f.condition}: {f.message} — hint: {f.hint}" for f in findings]
    joined = "\n  ".join(lines)
    return UserInputError(f"Condition errors for {source}:\n  {joined}")


def _collect_interactive_answers(
    blueprint: Blueprint,
    *,
    already_supplied: dict[str, str],
) -> dict[str, object]:
    """Prompt for every blueprint variable not already supplied by a flag."""
    answers: dict[str, object] = {}
    for var in blueprint.variables:
        if var.id in already_supplied:
            continue
        validator = _prompt_validator(var)
        if var.type.value == "boolean":
            answers[var.id] = prompts.ask_confirm(f"{var.id}?", default=bool(var.default))
        elif var.type.value == "enum":
            choices = list(var.validation.enum) if var.validation and var.validation.enum else []
            answers[var.id] = prompts.ask_select(f"{var.id}", choices)
        else:
            answers[var.id] = prompts.ask_text(
                var.description or var.id,
                default=str(var.default) if var.default is not None else None,
                validator=validator,
            )
    return answers


def _prompt_validator(var: VariableDefinition) -> prompts.Validator | None:
    """Adapt a blueprint variable's rules to the prompt engine's signature."""
    if var.id not in NAMED_VALIDATORS:
        return None
    return _prompt_validator_for(var.id)


def _prompt_validator_for(named: str) -> prompts.Validator:
    """Adapt a named validator from ``resolution.names`` to the prompt engine."""
    validator = NAMED_VALIDATORS[named]

    def _validate(value: str) -> str | None:
        problem = validator(value)
        return None if problem is None else problem[1]

    return _validate


# -----------------------------------------------------------------------------
# Generation (plan → validate → backup → stage → commit)
# -----------------------------------------------------------------------------
def _generate(
    *,
    blueprint: Blueprint,
    inputs: _ResolvedInputs,
    conditional_groups: list[SurvivingGroup],
    dry_run: bool,
    overwrite: bool,
) -> None:
    """Run the plan-through-commit pipeline.

    Five phases, in order:

    1. **Plan** (``FileMapper.map``). Pure; no filesystem access.
    2. **Dry-run short-circuit.** If ``--dry-run``, render and return.
       No further phases run — dry-run has no side effects by contract.
    3. **Safety validation** (``forge.safety``). Reads the filesystem
       to check structure, existence, and collisions. Raises
       ``CollisionError`` or ``SafetyError`` (translated to the CLI's
       equivalents).
    4. **Backups** (``backup_existing``). Only when ``--overwrite``
       was passed *and* there are existing destinations to preserve.
       Atomic; rolls back on failure.
    5. **Staging and commit** (``StagingArea``). Renders every planned
       file into a staging directory, then renames them into place.
       Every write is inside staging until the final commit; every
       commit is a per-file atomic ``os.replace``.

    Ordering rationale: each phase is strictly more expensive than the
    previous, and each phase *fails closed* before the next one
    starts. A traversal attempt fails at phase 3 without any backup or
    staging directory having been created. A collision fails at phase
    3 before any backup runs, so the user sees the collision without
    paying the backup cost. Backups only run for files about to be
    replaced, and only after the plan is known-safe. Staging only
    runs after backups are complete, so a mid-commit failure has a
    recoverable state (the backups).

    Why the safety validation runs even when ``overwrite`` is True:
        ``--overwrite`` consents to *replacing existing files*. It
        does *not* consent to writing outside the root, following
        symlinks, or writing into an unwritable parent. Those
        structural refusals are unaffected by ``--overwrite``, so the
        safety check is the same in both cases; only the collision
        branch changes (it does not fire when ``overwrite`` is True).
    """
    # ------------------------------------------------------------------
    # Phase 1: plan.
    # ------------------------------------------------------------------
    plan_result = FileMapper().map(
        blueprint,
        inputs.variables.as_dict(),
        conditional_groups=conditional_groups,
    )
    if isinstance(plan_result, Err):
        raise _as_user_input_error_from_mapping(
            plan_result.error, source=f"blueprint:{inputs.blueprint_id}"
        )
    plan: list[PlannedFile] = plan_result.value

    # ------------------------------------------------------------------
    # Phase 2: dry-run short-circuit.
    # ------------------------------------------------------------------
    if dry_run:
        _render_plan(blueprint=blueprint, inputs=inputs, plan=plan)
        return

    # ------------------------------------------------------------------
    # Phase 3: safety validation.
    #
    # Two passes:
    #
    #   a. Per-file validation — every planned destination must be
    #      structurally safe (``safe_resolve``) and ready to write
    #      (``validate_destination``). Findings are aggregated, then
    #      translated into the CLI's error types.
    #
    #   b. Plan-vs-disk collision detection — with ``--overwrite``
    #      consent threaded through, so the safety layer can decide
    #      which collisions to raise and which to allow (backups in
    #      phase 4 preserve the overwritten data either way).
    #
    # A structural failure in (a) precludes (b) because there is no
    # sensible collision check to run against an unsafe destination.
    # ------------------------------------------------------------------
    safety_findings: list[_SafetyFinding] = []
    for planned in plan:
        resolved, finding = safe_resolve(planned.destination, inputs.destination)
        if finding is not None:
            safety_findings.append(finding)
            continue
        # ``safe_resolve`` returns ``(Path, None)`` on success and
        # ``(None, Finding)`` on failure. The ``finding is not None``
        # branch above handles the failure case and ``continue``s;
        # after the ``continue``, ``resolved`` must be the non-``None``
        # half of the tuple. mypy's flow analysis cannot infer that on
        # its own (the return type is a union of two-element tuples,
        # not a union of element types), so the check below is both a
        # type-narrowing hint for mypy *and* a runtime contract check.
        #
        # A real ``RuntimeError`` — not an ``assert`` — because the
        # check must survive ``python -O`` (which strips asserts) and
        # because a ``(None, None)`` return would be a Forge bug, not
        # a user-facing condition. The earlier ``assert`` was silent
        # under ``-O``; the subsequent ``validate_destination(None,
        # ...)`` call would have failed with a confusing
        # ``AttributeError`` from deep inside the safety layer instead
        # of the loud, located error below.
        if resolved is None:
            raise RuntimeError(
                "safe_resolve returned (None, None) — a contract violation "
                f"for destination {planned.destination!r}. This is a bug in "
                "forge.safety; please report it."
            )
        safety_findings.extend(validate_destination(resolved, overwrite=overwrite))
    safety_findings.extend(detect_collisions(plan, inputs.destination, overwrite=overwrite))

    if safety_findings:
        _raise_safety_error(safety_findings, operation="create", destination=inputs.destination)

    # ------------------------------------------------------------------
    # Phase 4: backups (opt-in via --overwrite, and only for existing
    # files).
    #
    # We compute the "targets to back up" set from the plan *plus* the
    # destination root, not from ``safety_findings`` — because the
    # safety layer already confirmed which destinations exist (in its
    # own way). Re-deriving the existing files here keeps this function
    # honest about what it is about to replace: the plan is the source
    # of truth, and ``lstat`` on each target is the check.
    # ------------------------------------------------------------------
    if overwrite:
        existing_targets = _existing_targets(plan, inputs.destination)
        if existing_targets:
            try:
                backup_existing(existing_targets)
            except OSError as exc:
                raise FilesystemConflictError(
                    f"Could not back up existing files before overwrite: {exc}",
                    hint=(
                        "Check permissions and free disk space, then retry. "
                        "No files have been modified."
                    ),
                ) from exc

    # ------------------------------------------------------------------
    # Phase 5: staging and commit.
    #
    # Two sub-phases:
    #
    #   a. Render every planned file into a staging directory. All
    #      rendering happens here; a template failure aborts the whole
    #      operation before any commit.
    #
    #   b. Commit: move each staged file to its destination via
    #      ``StagingArea.commit``. Each move is a per-file atomic
    #      ``os.replace``.
    #
    # ``StagingArea.for_root`` creates a hidden ``.forge-stage-*``
    # directory *inside* the destination root (same filesystem, so
    # ``os.replace`` is atomic). The context manager removes it on
    # exit, including on exception. Phase 5 is the *only* phase that
    # mutates the destination root; phases 1-4 are read-only against
    # the destination (phase 4 writes only to backup files, which are
    # new and outside the plan's target set).
    # ------------------------------------------------------------------
    renderer = TemplateRenderer()
    blueprint_root = _blueprint_root(blueprint, inputs.blueprint_id)

    # Ensure destination root exists before staging: ``mkdtemp`` (used
    # by ``StagingArea.for_root``) requires its parent to exist.
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
        f"Created {inputs.project_name!r} from {inputs.blueprint_id} "
        f"({len(committed)} file(s))."
    )


def _raise_safety_error(
    findings: list[_SafetyFinding],
    *,
    operation: str,
    destination: Path,
) -> None:
    """Translate safety findings into the CLI's error hierarchy.

    ``forge.safety.errors.Finding`` is a dataclass with fields
    ``(code, path, message, hint, root)``. The CLI's ``SafetyError``
    and ``CollisionError`` expect a four-tuple projection
    ``(code, path, message, hint)``. This function does that
    translation, then raises the *correct* type based on the findings'
    codes:

    * If any finding is a collision code (``collision.exists`` or
      ``plan.case_insensitive_duplicate``), raise
      ``core.exceptions.CollisionError`` (exit code 6).
    * Otherwise, raise ``core.exceptions.SafetyError`` (exit code 3).

    The type is what makes the CLI handler offer ``--overwrite`` — a
    structural safety failure never gets that hint, because the flag
    could not help.

    Note: this function imports ``forge.core.exceptions`` for its
    output types, not its input types. ``forge.safety`` remains a
    primitive that does not import ``forge.core`` (dependency
    direction: primitive → nobody).

    ``destination`` is accepted for parity with the finding-shape
    contract but is not consumed here — the findings' own ``root``
    field carries the same information, and the CLI handler renders
    it per-finding. The parameter is kept because a future revision
    that wanted to summarise the operation ("failed to write to
    <destination>") would need it, and removing it now would be a
    signature change for no gain.
    """
    projected = tuple((f.code, f.path, f.message, f.hint) for f in findings)
    collision_codes = {"collision.exists", "plan.case_insensitive_duplicate"}
    is_collision = any(f.code in collision_codes for f in findings)

    if is_collision:
        raise CoreCollisionError(findings=projected, operation=operation)
    raise CoreSafetyError(findings=projected, operation=operation)


def _existing_targets(plan: list[PlannedFile], root: Path) -> list[Path]:
    """Return the absolute paths of plan destinations that exist on disk.

    Uses ``lstat`` (via ``Path.exists()``'s pre-check is *not*
    sufficient — a broken symlink would be missed). The list is
    filtered by lstat so a broken symlink is included (it is a name
    in use that the overwrite will replace).

    Kept as a small helper (rather than inline in ``_generate``) so
    the "what does 'existing' mean?" decision is in one place and
    matches the safety layer's definition (``_lstat_exists``).
    """
    import os

    existing: list[Path] = []
    for planned in plan:
        target = root / planned.destination
        try:
            os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError:
            # Fail closed: treat unreadable entries as existing.
            # A spurious backup is cheap; a missed backup is data loss.
            pass
        existing.append(target)
    return existing


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
    """Render or copy one planned file into staging.

    * ``COPY`` — read bytes verbatim from the blueprint's template
      directory and write them to staging. The bytes are *not* passed
      through the renderer, so a template-looking file that the
      blueprint opted out of rendering (via ``.png`` extension or
      non-``.j2`` source) is copied byte-for-byte.
    * ``RENDER`` — render through ``TemplateRenderer`` with the
      resolved variables. Any ``TemplateRenderError`` is translated
      into the CLI's error hierarchy and raised.

    The blueprint root is resolved once by ``_generate`` and passed
    in, so this function has no filesystem-touching surprises beyond
    the two operations it performs (read source, write to staging).

    ``variables`` is typed ``dict[str, Any]`` rather than a looser
    ``dict`` because the underlying renderer consumes a mapping of
    ``str`` to arbitrary values, and an unparameterised ``dict`` would
    be ``dict[Any, Any]`` — which mypy strict flags as missing type
    parameters. The dict comes from ``ResolvedValues.as_dict()``,
    which is exactly that shape.
    """
    source_path = blueprint_root / planned.source

    if planned.is_render:
        result = renderer.render(source_path, variables)
        if not result.is_ok():
            raise _as_template_error(result.error, source=str(source_path))
        stage.write_text(planned.destination, result.value)
    else:
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            raise FilesystemConflictError(
                f"Could not read source file {planned.source!r} for copy: {exc}",
                hint="Check the blueprint's file entry; the source must be readable.",
            ) from exc
        stage.write_bytes(planned.destination, data)


def _as_template_error(error: _RenderError, *, source: str) -> TemplateRenderError:
    """Translate ``forge.rendering.errors.TemplateRenderError`` to the CLI type.

    The rendering-side type carries ``template``, ``line``, ``col``,
    and ``name`` — fields the CLI's ``TemplateRenderError`` does not
    have (it is a ``StructuredError`` with ``source`` and ``errors``).
    This function projects them into a single ``(pointer, message)``
    error tuple so the CLI handler can render it uniformly.

    The ``source`` parameter lets the caller name the *actual template
    path* even when the error's own ``template`` field would be the
    same — keeping the two in sync but allowing future callers (e.g.
    a batch renderer) to attribute errors more precisely.

    The parameter type is the renderer's own ``TemplateRenderError``,
    imported under ``TYPE_CHECKING`` as ``_RenderError``. The
    runtime value is that class; the import is type-only so the CLI
    does not pay for importing the full rendering error module at
    startup (the same policy ``forge.rendering``'s façade applies to
    the renderer itself). The name is aliased with a leading
    underscore so the file's own ``TemplateRenderError`` (the CLI's)
    remains the primary name in scope — a reader scanning the
    function signature should not have to disambiguate which
    ``TemplateRenderError`` is which.
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


def _blueprint_root(blueprint: Blueprint, blueprint_id: str) -> Path:
    """Return the directory the blueprint's templates are relative to.

    The blueprint's ``id`` is not itself a path; the loader resolves
    the blueprint document from a directory whose layout is
    ``<blueprints_root>/<blueprint_id>/blueprint.yaml``. The
    ``_blueprint_root`` is the *directory of the manifest* — the
    anchor for every ``source`` path in the plan.

    We re-resolve it via the registry (which owns discovery), rather
    than caching it on ``Blueprint``. The blueprint model is a pure
    document (ADR-0007/0008); threading a filesystem path into it
    would couple the schema to the loader's layout, which is exactly
    the coupling the two-accessor registry design avoids for
    ``get_blueprint`` vs ``get_blueprint_document``.

    A ``DiscoveryError`` from the registry (blueprint vanished between
    load and generate — a race) is the only failure mode here; it is
    a filesystem conflict at the CLI boundary and is rendered with
    exit code 3.
    """
    # The registry's discovery service already knows how to find a
    # blueprint by id; reuse it rather than reimplementing the search.
    from forge.discovery import BlueprintDiscoveryService

    service = BlueprintDiscoveryService()
    with_path = service.load_with_path(blueprint_id)
    if with_path is None:
        raise FilesystemConflictError(
            f"Blueprint {blueprint_id!r} disappeared between load and generate.",
            hint=(
                "This is unexpected. Re-run the command; if it persists, "
                "check whether the blueprint file was moved or deleted."
            ),
        )
    path, _ = with_path
    return path.parent


# -----------------------------------------------------------------------------
# Dry-run rendering (unchanged)
# -----------------------------------------------------------------------------
def _render_plan(
    *,
    blueprint: Blueprint,
    inputs: _ResolvedInputs,
    plan: list[PlannedFile],
) -> None:
    """Render the plan as a human-readable listing for ``--dry-run``.

    Deliberately informational, not authoritative: the plan's
    authoritative form is the ``list[PlannedFile]`` the writer
    consumes. This rendering exists so a user can preview what would
    be written without committing to it.
    """
    ui.section("Generation plan (dry run)")
    ui.info(f"Blueprint:   {inputs.blueprint_id}")
    ui.info(f"Name:        {inputs.project_name}")
    ui.info(f"Destination: {inputs.destination}")
    ui.info("")

    ui.section(f"Files ({len(plan)})")
    for planned in plan:
        marker = "render" if planned.is_render else "  copy"
        ui.info(f"  [{marker}] {planned.destination}")
        ui.info(f"           ← {planned.source}")

    ui.info("")
    render_count = sum(1 for p in plan if p.is_render)
    copy_count = sum(1 for p in plan if not p.is_render)
    ui.info(f"Summary: {render_count} rendered, {copy_count} copied")


def _as_user_input_error_from_mapping(error: MappingError, *, source: str) -> UserInputError:
    """Convert a planner ``MappingError`` into a ``UserInputError``."""
    if not error.findings:
        return UserInputError(f"Planning failed for {source} (no details available).")
    joined = "\n  ".join(f.format() for f in error.findings)
    return UserInputError(f"Planning failed for {source}:\n  {joined}")


# -----------------------------------------------------------------------------
# Internal result type (unchanged)
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _ResolvedInputs:
    """Validated, immutable inputs for the generation pipeline.

    ``variables`` is a ``ResolvedValues``, not a ``dict``.
    Immutability is a correctness property: the resolved values cross
    the boundary between "user input collection" and "filesystem
    mutation", and a downstream handler that could mutate
    ``variables`` would be mutating a *validated* mapping — the
    invariant ``ResolvedValues`` exists to protect.
    """

    blueprint_id: str
    project_name: str
    destination: Path
    variables: ResolvedValues


# -----------------------------------------------------------------------------
# Public re-exports (unchanged)
# -----------------------------------------------------------------------------
__all__ = ["app"]
