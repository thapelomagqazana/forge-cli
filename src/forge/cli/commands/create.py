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

Both modes converge on ``verification.handlers.validate_blueprint_inputs``.
That is the *single* validation funnel: adding a rule means editing
one function, not two call sites. If flag validation and prompt
validation ever diverged, the two modes would accept different
projects — the exact technical debt this design exists to prevent.

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
"""

from __future__ import annotations

from pathlib import Path

import typer

from forge.cli import prompts, ui
from forge.cli.registry import get_blueprint
from forge.core.exceptions import (
    FilesystemConflictError,
    MissingRequiredFlagsError,
)

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
        # The requirement is enforced in ``_resolve_inputs`` below,
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
    inputs = _resolve_inputs(
        blueprint=blueprint,
        name=name,
        output=output,
        no_interactive=no_interactive,
    )
    _generate(inputs, dry_run=dry_run, overwrite=overwrite)


# -----------------------------------------------------------------------------
# Input resolution
def _resolve_inputs(
    *,
    blueprint: str,
    name: str | None,
    output: Path,
    no_interactive: bool,
) -> _ResolvedInputs:
    """Collect and validate all inputs for the ``create`` command.

    Two modes, one output type:

    - **Non-interactive** — require ``--name``; if absent, raise
      ``MissingRequiredFlagsError`` listing the gap. This is the
      "fail fast, fail complete" contract for scripted callers.
    - **Interactive** — prompt for anything not supplied as a flag.

    In both cases the collected values flow through
    ``validate_blueprint_inputs`` (the single funnel). The mode
    determines *where values come from*, not *what counts as valid* —
    that distinction is the whole design.

    Args:
        blueprint: The blueprint ID passed as a positional argument.
            Always required (Typer enforces it).
        name: The project name from ``--name``, or ``None`` if
            omitted. Presence is enforced by mode, not by Typer.
        output: The destination directory from ``--output``.
        no_interactive: ``True`` if the user passed
            ``--no-interactive``. When ``False``, the mode also
            depends on ``prompts.is_interactive()``.

    Returns:
        A validated, immutable ``_ResolvedInputs`` ready for
        generation.

    Raises:
        MissingRequiredFlagsError: Non-interactive mode, missing
            ``--name``.
        PromptCancelledError: Interactive mode, user cancelled.
        UserInputError: Any value failed validation.
        BlueprintError: The blueprint ID is not known.
    """
    # Load the blueprint *first*, before any prompts. Two reasons:
    #   1. A bad blueprint ID fails fast with exit code 2, before we
    #      make the user answer questions for a project that cannot
    #      be generated.
    #   2. The blueprint's variable descriptors drive the wizard's
    #      later prompts, so we need them regardless of mode.
    blueprint_entry = get_blueprint(blueprint)

    interactive = prompts.is_interactive() and not no_interactive

    if not interactive:
        # Non-interactive: aggregate every missing flag into one
        # error. The helper does the aggregation *and* the type
        # narrowing — no ``assert`` needed, so the guarantee survives
        # ``python -O`` (which strips asserts and would otherwise
        # let ``None`` reach the funnel as a silent bug).
        collected = _require_string_flags(
            command="create",
            values={"--name": name},
        )
        resolved_name = collected["--name"]
    else:
        # Interactive: flags win. If the user already supplied
        # ``--name``, do not re-ask — muscle memory matters, and a
        # wizard that re-asks answered questions feels broken.
        resolved_name = name or prompts.ask_text(
            "Project name",
            validator=_project_name_validator(),
        )

    # Delegate validation to the verification layer. The wizard and
    # the flag path both land here, so a rule added here applies to
    # both modes with no risk of drift.
    #
    # The import is *attribute access on the module*, not a bound
    # name (``from X import Y``), for two reasons:
    #
    # 1. **Testability.** A test can substitute
    #    ``forge.verification.handlers.validate_blueprint_inputs``
    #    without worrying about import order, and the substitution
    #    takes effect on the next call.
    # 2. **Failure locality.** If the function is missing, the error
    #    is an ``AttributeError`` at *this* line with a traceback
    #    pointing here — not an ``ImportError`` at import time with
    #    no indication of which caller needed it.
    from forge.verification import handlers

    validated = handlers.validate_blueprint_inputs(
        blueprint_id=blueprint_entry.id,
        name=resolved_name,
        destination=str(output / resolved_name),
        variables={},  # blueprint variables land in a later task
    )

    return _ResolvedInputs(
        blueprint_id=validated.blueprint_id,
        project_name=validated.name,
        destination=Path(validated.destination),
        variables=dict(validated.variables),
    )


def _require_string_flags(
    *,
    command: str,
    values: dict[str, str | None],
) -> dict[str, str]:
    """Return ``values`` with non-``None`` guarantees, or raise.

    Aggregates *every* missing flag into one
    ``MissingRequiredFlagsError`` (fail fast, fail complete) and
    narrows the type for the caller in the same call — so no
    downstream code needs ``assert`` (which would silently vanish
    under ``python -O``) or ``cast`` (which would lie to mypy if the
    branch logic ever changed).

    Why not ``assert``: ``S101`` (flake8-bandit) forbids ``assert``
    in production code because ``python -O`` strips it. The
    ``assert name is not None`` that this helper replaces would have
    *looked* like it enforced the missing-flags contract, but under
    ``-O`` it would have let ``None`` flow into
    ``validate_blueprint_inputs``, producing an unrelated
    "invalid project name" error instead of the correct "missing
    --name" error. The helper's narrowing is real at runtime — the
    dict comprehension drops ``None`` values — so it survives
    optimization.

    Args:
        command: The command name (e.g., ``"create"``). Rendered in
            the ``MissingRequiredFlagsError`` message so the user
            knows which command needs fixing.
        values: Mapping from flag name (``"--name"``) to the value
            the user supplied, or ``None`` if they did not supply
            one. Flag names are kept with their leading dashes so
            the error can render them verbatim — see
            ``MissingRequiredFlagsError`` for the formatting.

    Returns:
        A new mapping with the same keys and ``str`` values. The
        caller can access any key without a ``None`` check; mypy
        accepts the access because the return type is
        ``dict[str, str]``.

    Raises:
        MissingRequiredFlagsError: If any value in ``values`` is
            ``None``. The error lists *every* missing flag, not just
            the first — a caller fixing two flags in one edit is
            dramatically better than a caller fixing them one
            command-run at a time.
    """
    # Build the complete missing list before raising. This is the
    # aggregate-everything property, and it is why the helper takes
    # the whole dict rather than one flag at a time.
    missing = [flag for flag, value in values.items() if value is None]
    if missing:
        raise MissingRequiredFlagsError(
            command=command,
            missing=tuple(flag.lstrip("-").replace("-", "_") for flag in missing),
        )
    # After the raise, every value is provably a ``str``. The dict
    # comprehension narrows the type; mypy accepts it because the
    # ``if missing`` branch above is the only path that could produce
    # a ``None`` value, and it raises. Unlike ``assert``, this
    # narrowing is real at runtime — it does not depend on ``-O``.
    return {flag: value for flag, value in values.items() if value is not None}


def _project_name_validator() -> prompts.Validator:
    """Return a validator for the interactive project-name prompt.

    Real-time feedback here is the highest-leverage UX detail in the
    wizard: the user learns the rule while typing, not after a failed
    scaffold. The rules are intentionally minimal — the *authoritative*
    validation is still ``validate_blueprint_inputs``. This validator
    exists only to catch the obvious cases inline so the user is not
    surprised later.

    Why duplicate at all: catching a bad name *after* a prompt loop
    is a worse experience than catching it *during* the prompt. The
    duplication is a UX affordance, not a second source of truth —
    anything this validator accepts, the authoritative validator must
    also accept (enforced by a test in ``tests/unit/cli/test_create_*``).
    """

    def _validate(value: str) -> str | None:
        if not value:
            return "Project name cannot be empty."
        if not value.replace("-", "").replace("_", "").isalnum():
            return "Use only letters, digits, hyphens, and underscores."
        return None

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
        inputs: Validated inputs from ``_resolve_inputs``.
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
class _ResolvedInputs:
    """Validated, immutable inputs for the generation pipeline.

    A frozen dataclass in spirit: it is passed across thread
    boundaries in future async pipelines and stored in test fixtures,
    so immutability is a correctness property, not a style choice.
    The underscore prefix marks it as private to this module — the
    command's *public* contract is the exit codes and the CLI surface,
    not the internal result type. If a future task needs to share this
    type (e.g., with a ``--dry-run --json`` output), promote it then,
    with a real name and a real contract; do not pre-emptively export.
    """

    __slots__ = ("blueprint_id", "destination", "project_name", "variables")

    blueprint_id: str
    project_name: str
    destination: Path
    variables: dict[str, str]

    def __init__(
        self,
        *,
        blueprint_id: str,
        project_name: str,
        destination: Path,
        variables: dict[str, str],
    ) -> None:
        # Manual __init__ rather than dataclass(frozen=True) so we can
        # keep ``__slots__`` (frozen dataclasses cannot combine with
        # __slots__ before Python 3.10, and even then only with the
        # ``slots=True`` keyword — manual is clearer for a 4-field
        # type).
        object.__setattr__(self, "blueprint_id", blueprint_id)
        object.__setattr__(self, "project_name", project_name)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "variables", dict(variables))

    def __setattr__(self, name: str, value: object) -> None:
        # Enforce immutability. A caught _ResolvedInputs must not be
        # mutated by a downstream handler — the wizard's answers are
        # a snapshot, not a scratchpad.
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ResolvedInputs):
            return NotImplemented
        return (
            self.blueprint_id == other.blueprint_id
            and self.project_name == other.project_name
            and self.destination == other.destination
            and self.variables == other.variables
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.blueprint_id,
                self.project_name,
                self.destination,
                frozenset(self.variables.items()),
            )
        )

    def __repr__(self) -> str:
        return (
            f"_ResolvedInputs(blueprint_id={self.blueprint_id!r}, "
            f"project_name={self.project_name!r}, "
            f"destination={self.destination!r})"
        )


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# Only the module-level ``app`` is public. The registry mounts it;
# everything else is an implementation detail of this command.
__all__ = ["app"]
