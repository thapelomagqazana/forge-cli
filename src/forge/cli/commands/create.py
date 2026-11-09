"""``forge create`` — create a project from a blueprint.

This command is the primary entry point for project generation. It
takes a blueprint ID (e.g., ``python.fastapi.api``), resolves it from
the discovered blueprints, resolves the variables (from CLI flags,
interactive prompts, or blueprint defaults), runs the generation
pipeline (staging → validation → commit), and renders a summary.

Dual-mode input handling
------------------------
The command supports two input modes, dispatched once at the top of
``create`` (see ADR-0006):

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

A ``Typer`` with a callback and no subcommands is the canonical
"single-command app" shape. **The registry (``forge.cli.registry``)
detects this shape and registers the callback's function directly on
the root app as a leaf command**, under the name in ``app.info.name``.
That is what makes ``forge create python.fastapi.api --name my-api``
parse with ``python.fastapi.api`` as the callback's ``blueprint``
argument — not as a subcommand.

The ``invoke_without_command`` and ``no_args_is_help`` constructor
flags on ``typer.Typer`` are **not** the mechanism for this. That
flag only controls whether the callback runs when a subcommand *is*
invoked with no arguments; it does not remove the ``COMMAND
[ARGS]...`` slot from the app's signature. Placing it on the
constructor or on the decorator produces the same misleading
``Missing argument 'BLUEPRINT'`` error either way. The actual fix is
in the registry's ``_is_single_command_app``; this file only needs to
expose the shape.

Do **not** "simplify" this by converting ``@app.callback()`` to
``@app.command()`` and adding a second ``Typer`` layer, or by
re-adding ``invoke_without_command`` in the hope that it fixes the
nesting. It does not, and the previous two test runs are the proof.

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
wizard, to the domain, where it can coexist with the wizard. This is
deliberate and load-bearing; do not "simplify" it back to ``...``.

Why the project name is validated *outside* the resolver
--------------------------------------------------------
The resolver's contract is "resolve the variables the *blueprint*
declares" (ADR-0011). ``project_name`` is not one of them — it is a
command argument that happens to share a name with a well-known
variable id. Threading it through the resolver would either:

- require every blueprint to declare a variable it does not
  semantically own (a schema-level change that breaks every
  blueprint that does not use the name), or
- leave ``resolved["project_name"]`` as a ``KeyError`` for any
  blueprint that does not (the exact bug an earlier revision of
  this command hit).

So the project name is collected here and validated here, using the
*same* named validator (``resolution.names.project_name``) that the
resolver would apply if ``project_name`` were a blueprint variable.
One rule, two enforcement points — interactive prompt (real-time
UX) and non-interactive check (scripted correctness) — both backed
by the same function. That is the invariant this command must
maintain, and the tests pin it in both modes.

Why the command calls ``get_blueprint_document``, not ``get_blueprint``
---------------------------------------------------------------------
The registry exposes *two* blueprint accessors because two callers
have different needs:

* ``get_blueprint(blueprint_id) -> BlueprintEntry`` — a catalogue
  view (``id``, ``display_name``, raw ``variables`` tuple).
  Sufficient for ``forge list`` and for the wizard's *selection*
  menu; **not** sufficient to resolve values against (the resolver
  needs a ``Blueprint``, not an entry).

* ``get_blueprint_document(blueprint_id) -> Blueprint`` — the
  loaded, schema-validated, model-validated document. That is what
  any caller that *does something with* the blueprint needs:
  resolving variables (here), rendering templates (future),
  verifying checks (future).

``create`` needs the second. An earlier revision of this file called
``get_blueprint`` (the entry accessor) and then tried to reach
through ``entry.path`` to the loader — but ``BlueprintEntry``
deliberately has no ``path`` attribute (it is a catalogue view, not
a filesystem handle), and the attempt failed with
``AttributeError: 'BlueprintEntry' object has no attribute 'path'``.
The fix is to use the accessor that matches the need, not to widen
the entry's shape. Widening ``BlueprintEntry`` would drag the
loader's types into the catalogue view — the exact coupling the
two-accessor design exists to prevent.

The two accessors share the same ``_make_discovery_service()`` seam
in the registry, so tests patch *one* thing and both are
substituted. The distinction is about *what the caller gets back*,
not about *where the data comes from*.

Why no ``try`` / ``except`` around the command body
--------------------------------------------------
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
The flow is:

    blueprint  ──┐
    cli_vars   ──┼──►  VariableResolver.resolve  ──►  ResolvedValues
    prompts    ──┘                                    (immutable,
                                                       validated,
                                                       typed)

``VariableResolver.resolve`` applies the full precedence chain
(CLI flags > prompt answers > blueprint defaults) and validates each
value against its ``VariableDefinition`` (type coercion, pattern,
enum, named validators). Its output type ``ResolvedValues`` is
immutable and resolver-owned, so downstream code (the renderer, the
generator) can require it in their signatures and be confident that
unvalidated input never reaches them — the KPI this command exists
to deliver.

The command does not call ``validate_blueprint_inputs`` any more.
The blueprint itself is validated by the *loader* when it is fetched
(``get_blueprint_document`` delegates to discovery, which delegates
to ``BlueprintLoader.load``, ADR-0010), so the document is already
schema- and model-valid by the time this command sees it.

Why ``create`` does not import the loader
-----------------------------------------
The loader is a discovery-layer concern (ADR-0010). ``create``
should not know how a blueprint document is obtained, only that it
*is* obtained. The registry is the command layer's façade over
discovery — it exposes ``get_blueprint_document`` precisely so that
commands can ask for a document without reaching into the loader.
An earlier revision of this file imported ``BlueprintLoader``
directly and called it with a path derived from the discovery
entry; that coupled ``create`` to the loader's API and to
``BlueprintEntry``'s shape simultaneously, and the coupling broke
the moment either changed. The current design keeps ``create``
importing exactly one discovery-layer name
(``get_blueprint_document``), which is the minimum surface it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import typer

from forge.cli import prompts, ui
from forge.cli.registry import get_blueprint_document
from forge.core.exceptions import (
    FilesystemConflictError,
    MissingRequiredFlagsError,
    UserInputError,
)
from forge.loading.errors import ValidationError
from forge.loading.result import Err
from forge.models import Blueprint, VariableDefinition
from forge.resolution import ResolvedValues, VariableResolver
from forge.resolution.names import NAMED_VALIDATORS

# -----------------------------------------------------------------------------
# Command module contract
# -----------------------------------------------------------------------------
# Every command module exposes a module-level ``app`` attribute of type
# ``typer.Typer``. The registry (``forge.cli.registry``) discovers this
# attribute and mounts it on the root CLI under ``name``.
#
# This app is a *single-command app*: it has a callback and no
# subcommands. The registry detects this shape and registers the
# callback as a leaf command — see the module docstring's "Why the
# callback, and how ``forge create`` becomes a leaf command" section
# for the full mechanism and for why the constructor's
# ``invoke_without_command`` flag is *not* the answer.
#
# The explicit ``name=`` and ``help=`` are what the user sees in
# ``forge --help`` and ``forge create --help``, so they must be
# deliberate, not incidental. The registry uses ``app.info.name`` as
# the command name — do not remove ``name=``.
app = typer.Typer(
    name="create",
    help="Create a project from a blueprint.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
# The callback *is* the command. Its arguments are the command's
# arguments; the registry registers this function directly as a leaf
# command under the name ``create``. Do not convert it to
# ``@app.command()`` — that would nest the command inside a group and
# change the invocation to ``forge create create <blueprint>``.
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
        # See the module docstring: this is intentionally *not* ``...``.
        # A parser-level requirement would block the interactive wizard.
        # The requirement is enforced in ``_resolve_variables`` below,
        # where the interactive fallback exists.
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
            "command fails if any target file already exists."
        ),
    ),
) -> None:
    """Create a new project from a blueprint.

    Exit code contract:

    - 0 — Project created successfully (or dry-run completed).
    - 1 — User error (missing required variable, invalid input,
      cancellation).
    - 2 — Blueprint error (not found, invalid, incompatible).
    - 3 — Filesystem conflict (collision, permission denied).
    - 5 — Internal error (unexpected exception).

    The ``--json`` flag will be added in the JSON output task, not
    here. This command does not declare it yet, so the flag's absence
    is not a breaking change when it lands.
    """
    inputs = _resolve_variables(
        blueprint_id=blueprint,
        cli_name=name,
        cli_vars=_parse_var_flags(var),
        output=output,
        no_interactive=no_interactive,
    )
    _generate(inputs, dry_run=dry_run, overwrite=overwrite)


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
            # Last-wins would be surprising; duplicate flags are almost
            # always a copy-paste mistake in a shell script.
            raise UserInputError(
                f"--var key {key!r} was provided more than once. "
                f"Provide each variable at most once."
            )
        parsed[key] = value
    return parsed


# -----------------------------------------------------------------------------
# Variable resolution
# -----------------------------------------------------------------------------
def _resolve_variables(
    *,
    blueprint_id: str,
    cli_name: str | None,
    cli_vars: dict[str, str],
    output: Path,
    no_interactive: bool,
) -> _ResolvedInputs:
    """Collect user input and resolve it through the variable resolver.

    Two modes, one resolver for *blueprint variables*, one explicit
    handling of the *project name* (a command argument, not a variable):

    - **Non-interactive** — ``--name`` is required
      (``MissingRequiredFlagsError``) and validated against the same
      ``project_name`` named validator the resolver uses. Every
      required blueprint variable must be supplied as ``--var``; the
      resolver aggregates *all* missing/invalid values into one
      ``Err(errors)`` so the user fixes everything in one edit.
    - **Interactive** — the wizard prompts only for what the flags did
      not supply. Flags win; supplied values are never re-asked. The
      prompt's inline validator gives real-time feedback using the
      same named validator.

    In both cases the collected blueprint inputs flow through
    ``VariableResolver.resolve`` — the single validation funnel for
    *blueprint variables* (ADR-0011). The project name is validated
    alongside, by the *same* named validator, so the two paths cannot
    disagree on what a legal name is.

    Args:
        blueprint_id: The blueprint ID passed as a positional argument.
            Always required (Typer enforces it).
        cli_name: The project name from ``--name``, or ``None`` if
            omitted. Presence is enforced by mode, not by Typer.
        cli_vars: Parsed ``--var`` flags as ``{key: value}``. Highest
            precedence in the resolver for *blueprint* variables.
        output: The destination directory from ``--output``.
        no_interactive: ``True`` if the user passed ``--no-interactive``.

    Returns:
        A validated, immutable ``_ResolvedInputs`` ready for generation.

    Raises:
        MissingRequiredFlagsError: Non-interactive mode, missing
            ``--name``.
        PromptCancelledError: Interactive mode, user cancelled.
        UserInputError: The project name or any blueprint variable
            failed validation.
        BlueprintError: The blueprint ID is not known.
    """
    # Load the blueprint *first*, before any prompts. Three reasons:
    #   1. A bad blueprint ID fails fast with exit code 2, before we
    #      make the user answer questions for a project that cannot
    #      be generated.
    #   2. The blueprint's variable definitions drive both the wizard's
    #      prompts and the resolver's validation rules, so we need the
    #      loaded document regardless of mode.
    #   3. ``get_blueprint_document`` returns the *loaded document*
    #      (``Blueprint``), which is what the resolver requires — not
    #      the catalogue view (``BlueprintEntry``) that ``forge list``
    #      uses. See the module docstring's "Why the command calls
    #      ``get_blueprint_document``, not ``get_blueprint``".
    blueprint = get_blueprint_document(blueprint_id)

    interactive = prompts.is_interactive() and not no_interactive

    # ------------------------------------------------------------------
    # 1. Resolve the project name — a *command argument*, not a blueprint
    #    variable. Collected here, validated here, by the same named
    #    validator the resolver would apply if ``project_name`` were a
    #    declared variable.
    # ------------------------------------------------------------------
    if not interactive:
        # Fail fast, fail complete: aggregate every missing flag into one
        # error so a scripted caller fixes them in a single edit. This
        # also produces the flag-shaped message (``--name``) rather than
        # the resolver's ``required.missing`` for a variable the
        # blueprint never declared.
        _require_flags(command="create", values={"--name": cli_name})
        # After ``_require_flags`` raises on ``None``, mypy still sees
        # ``cli_name: str | None``; the assertion narrows it. Under
        # ``python -O`` the assert is stripped, but the branch is
        # unreachable in that mode because ``_require_flags`` would
        # have raised first — the narrowing is structural, not
        # defensive.
        assert cli_name is not None  # noqa: S101
        project_name = cli_name
        # The project name is a command argument, so it does not flow
        # through ``VariableResolver.resolve`` — but it still must be
        # validated. Use the *same* named validator the wizard uses,
        # so a name that would be rejected interactively is also
        # rejected non-interactively. Without this step, an invalid
        # name would slip through to ``_generate`` and produce a
        # confusing "unexpected internal error" (exit 5) instead of
        # the correct "invalid project name" (exit 1).
        _validate_project_name(project_name)
    else:
        # Flags win: a supplied --name is never re-asked. Muscle memory
        # matters, and a wizard that re-asks answered questions feels
        # broken. The prompt's validator gives real-time feedback using
        # the same named validator the non-interactive path applies.
        project_name = cli_name or prompts.ask_text(
            "Project name",
            validator=_prompt_validator_for("project_name"),
        )

    # ------------------------------------------------------------------
    # 2. Resolve the blueprint's own variables via the resolver.
    #    ``cli_vars`` contains only ``--var`` flags — NOT the project
    #    name (see "Why the project name is validated outside the
    #    resolver" in the module docstring).
    # ------------------------------------------------------------------
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
        # All errors aggregated by the resolver. Raise a single
        # ``UserInputError`` whose message joins every error's
        # ``format()`` rendering, so the CLI handler can render them
        # all with one exception.
        raise _as_user_input_error(result.error, source=f"blueprint:{blueprint_id}")

    # ------------------------------------------------------------------
    # 3. Combine: project_name (command arg) + resolver output
    #    (blueprint variables). The result is a fresh, immutable
    #    ``ResolvedValues`` so downstream code continues to get the
    #    compile-time guarantee — a plain ``dict`` here would silently
    #    undo the KPI for anything that reads ``inputs.variables``.
    # ------------------------------------------------------------------
    combined = dict(result.value.as_dict())
    combined["project_name"] = project_name

    return _ResolvedInputs(
        blueprint_id=blueprint.id,
        project_name=project_name,
        destination=output / project_name,
        variables=ResolvedValues(MappingProxyType(combined)),
    )


def _validate_project_name(value: str) -> None:
    """Reject ``value`` with ``UserInputError`` if it fails the shared rule.

    Uses the *same* named validator (``resolution.names.project_name``)
    that the wizard and the resolver use, so the three enforcement
    points cannot disagree about what counts as a legal name.

    The exception is a ``UserInputError`` — the domain class the CLI
    handler routes to exit code 1 with a friendly message. That is the
    exact exception shape the wizard's prompt loop would have produced
    (the prompt engine's ``_guard`` raises ``PromptCancelledError`` on
    cancellation, but a *validation* failure is re-prompted rather
    than raised; here in non-interactive mode, we raise directly
    because there is no loop to re-prompt into).
    """
    problem = NAMED_VALIDATORS["project_name"](value)
    if problem is None:
        return
    message, hint = problem
    # ``UserInputError`` folds message and hint into one string for the
    # CLI handler to render. Keeping the hint adjacent to the message
    # (rather than as a separate field) is the current contract of
    # that exception — if it ever grows a ``hint`` attribute, this
    # call site is the one that simplifies.
    raise UserInputError(f"{message} Hint: {hint}")


def _require_flags(*, command: str, values: dict[str, str | None]) -> None:
    """Raise ``MissingRequiredFlagsError`` if any value is ``None``.

    Aggregates every missing flag into a single error (fail fast, fail
    complete). An ``assert``-based narrowing would vanish under
    ``python -O``; this helper simply *raises*, which survives any
    optimization flag.
    """
    missing = [flag for flag, value in values.items() if value is None]
    if missing:
        raise MissingRequiredFlagsError(
            command=command,
            missing=tuple(flag.lstrip("-").replace("-", "_") for flag in missing),
        )


def _as_user_input_error(errors: list[ValidationError], *, source: str) -> UserInputError:
    """Convert resolver errors into one ``UserInputError`` for the handler.

    The CLI handler renders the exception's message; until a structured
    error renderer exists, we join the formatted errors so the user
    sees every problem in one pass. ``ValidationError.format()``
    produces a single-line rendering that already carries the hint,
    so joining them preserves the actionable text for each error.

    ``source`` is threaded into the message so batch callers (a future
    ``forge validate`` invocation over many files) can attribute the
    error to the right input. For a single ``create`` invocation it is
    always the blueprint id, but the signature keeps the call site
    honest about what it is attributing.
    """
    if not errors:
        return UserInputError(f"Input validation failed for {source} (no details available).")
    joined = "\n  ".join(err.format() for err in errors)
    return UserInputError(f"Input validation failed for {source}:\n  {joined}")


def _collect_interactive_answers(
    blueprint: Blueprint,
    *,
    already_supplied: dict[str, str],
) -> dict[str, object]:
    """Prompt for every blueprint variable not already supplied by a flag.

    Flags win, always: a variable present in ``already_supplied`` is
    skipped, so a user who typed ``--var author=alice`` is never
    asked for the author again. This is the behaviour that makes the
    wizard feel like an *assistant*, not a re-asking form.

    Each prompt wires the same named validator the resolver uses (for
    ids that have one), so a value the wizard accepts will not be
    rejected by the resolver later, and vice versa. Pattern and enum
    checks run in the resolver after the prompt returns — the prompt
    engine only offers real-time feedback for *named* validators
    because they are pure string predicates, which is the honest
    boundary (see ``_prompt_validator``).
    """
    answers: dict[str, object] = {}
    for var in blueprint.variables:
        if var.id in already_supplied:
            continue  # flag wins; never re-ask
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
    """Adapt a blueprint variable's rules to the prompt engine's signature.

    Only *named* validators can give real-time feedback today, because
    they are pure string predicates. Pattern and enum checks are
    applied by the resolver after the prompt returns — acceptable UX
    for v1 (the user gets the error before scaffolding) and honest:
    we do not pretend a check the prompt cannot perform.

    The named-validator lookup is delegated to
    ``_prompt_validator_for`` so the wizard and the non-interactive
    path share exactly one adapter implementation. Two call sites, one
    function — the same "one rule, one source" principle the
    resolution layer exists to enforce.
    """
    if var.id not in NAMED_VALIDATORS:
        return None
    return _prompt_validator_for(var.id)


def _prompt_validator_for(named: str) -> prompts.Validator:
    """Adapt a named validator from ``resolution.names`` to the prompt engine.

    The prompt engine accepts a validator that returns ``None`` on
    success or a message string on failure (``cli.prompts.Validator``).
    The named validators in ``resolution.names`` return ``None`` or a
    ``(message, hint)`` tuple. This adapter picks the *hint* as the
    user-facing message, because the hint is the actionable text and
    real-time feedback should suggest the fix, not merely restate the
    problem.

    ``KeyError`` from a missing validator name is deliberately not
    caught: the caller passes a name defined in ``resolution.names``,
    and a typo is a programming error, not a user-facing condition.
    """
    validator = NAMED_VALIDATORS[named]

    def _validate(value: str) -> str | None:
        problem = validator(value)
        return None if problem is None else problem[1]

    return _validate


# -----------------------------------------------------------------------------
# Generation hand-off
# -----------------------------------------------------------------------------
def _generate(
    inputs: _ResolvedInputs,
    *,
    dry_run: bool,
    overwrite: bool,
) -> None:
    """Run the generation pipeline.

    Kept as a separate function so tests can monkeypatch it to assert
    on the resolved inputs without exercising the filesystem. The
    real implementation lands in the generation-pipeline task; this
    version fails loudly with a TODO so it cannot be mistaken for a
    working generator.

    Args:
        inputs: Validated inputs from ``_resolve_variables``.
        dry_run: If ``True``, print the plan and exit without writing.
        overwrite: If ``True``, allow overwriting existing files.

    Raises:
        FilesystemConflictError: The destination exists and
            ``overwrite`` is ``False`` *and* this is not a dry run.
    """
    # The pre-flight conflict check is skipped for dry runs. A dry
    # run is a *plan*, not a *commit*; refusing to render the plan
    # because the target is occupied prevents the user from ever
    # seeing what would be blocked — which defeats the purpose of
    # asking for the plan. The future plan output will list the
    # conflict explicitly; today it just prints the intended
    # destination and lets the user infer the conflict.
    #
    # For a non-dry run, the check is a pre-flight — the pipeline
    # re-checks at commit time, because files can appear between the
    # pre-flight and the commit.
    if not dry_run and inputs.destination.exists() and not overwrite:
        raise FilesystemConflictError(
            f"Destination {str(inputs.destination)!r} already exists.",
            hint="Choose a different --output, or pass --overwrite.",
        )

    if dry_run:
        ui.section("Generation plan (dry run)")
        ui.info(f"Blueprint:   {inputs.blueprint_id}")
        ui.info(f"Name:        {inputs.project_name}")
        ui.info(f"Destination: {inputs.destination}")
        if len(inputs.variables):
            ui.info("Variables:")
            for key in sorted(inputs.variables):
                ui.info(f"  {key} = {inputs.variables[key]}")
        return

    # TODO: call into ``forge.generation.scaffold_project`` once the
    # generation pipeline lands. Until then, this command cannot
    # silently no-op — a no-op would look like success to a script
    # and produce an empty directory the user cannot debug. Raise a
    # clear internal error so the missing implementation is loud.
    raise NotImplementedError(
        "The generation pipeline is not yet implemented. "
        "This command is wired for dual-mode input handling only; "
        "see the generation-pipeline task for the scaffold hand-off."
    )


# -----------------------------------------------------------------------------
# Internal result type
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _ResolvedInputs:
    """Validated, immutable inputs for the generation pipeline.

    Immutability is a correctness property, not a style choice: the
    resolved values cross the boundary between "user input collection"
    and "filesystem mutation", and a downstream handler that could
    mutate ``variables`` would be mutating a *validated* mapping —
    which is precisely the invariant ``ResolvedValues`` exists to
    protect.

    ``variables`` is a ``ResolvedValues``, not a ``dict``. An earlier
    revision annotated it as ``ResolvedValues`` while assigning a
    plain ``dict`` — a type lie that would have let a caller mutate
    the mapping without mypy noticing. The combined mapping
    (resolver output + ``project_name``) is wrapped in
    ``ResolvedValues`` before construction so the guarantee holds
    end-to-end.
    """

    blueprint_id: str
    project_name: str
    destination: Path
    variables: ResolvedValues


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# Only the module-level ``app`` is public. The registry mounts it;
# everything else is an implementation detail of this command.
__all__ = ["app"]
