"""``forge create`` — create a project from a blueprint.

This command is the primary entry point for project generation. It
takes a blueprint ID (e.g., ``python.fastapi.api``), resolves it from
the discovered blueprints, resolves the variables (from CLI flags,
interactive prompts, or blueprint defaults), runs the generation
pipeline (staging → validation → commit), and renders a summary.

At the current stage it is a stub: it prints a TODO message. The
module's structure — the module-level ``app``, the explicit ``name=``
and ``help=`` on the ``Typer`` constructor, the ``@app.command``
decorator, the argument and option contracts — is final. The
implementation of the body will be added in the generation pipeline
task.
"""

from __future__ import annotations

from pathlib import Path

import typer

# -----------------------------------------------------------------------------
# Command module contract
# -----------------------------------------------------------------------------
# Every command module exposes a module-level ``app`` attribute of type
# ``typer.Typer``. The registry (``forge.cli.registry``) discovers this
# attribute and mounts the app on the root CLI under ``name``.
#
# The explicit ``name=`` and ``help=`` are not strictly required (Typer
# falls back to the module name and an empty help string if omitted),
# but they are strongly preferred: the help text is what the user sees
# in ``forge --help`` and ``forge create --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="create",
    help="Create a project from a blueprint.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.command()
def create(
    blueprint: str = typer.Argument(
        ...,
        metavar="BLUEPRINT",
        help=(
            "Blueprint ID (e.g., 'python.fastapi.api'). Use 'forge list' "
            "to see available blueprints."
        ),
    ),
    name: str = typer.Option(
        ...,
        "--name",
        "-n",
        help=(
            "Project name. Must be lowercase, hyphenated, and a valid "
            "Python package name (e.g., 'payments-api')."
        ),
        # The project name is required in non-interactive mode (when
        # --no-interactive is passed or when stdin is not a TTY). In
        # interactive mode, the name will be prompted for if not
        # provided. Typer's `...` (Ellipsis) marks the option as
        # required, which is the correct contract for the CLI surface
        # — the wizard fills in the gaps, but the flag itself is
        # required at the flag layer.
        prompt=False,
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

    This is a stub. The real implementation will:

    1. Discover the requested blueprint (from bundled official
       blueprints and local custom blueprints). Fail with exit code 2
       (``ExitCode.BLUEPRINT_ERROR``) if the blueprint is not found.
    2. Resolve variables from CLI flags, interactive prompts (unless
       ``--no-interactive``), and blueprint defaults. Fail with exit
       code 1 (``ExitCode.USER_ERROR``) if a required value is missing
       or invalid.
    3. Build a generation plan (which files will be created, which
       will be skipped, and which will collide with existing files).
       If ``--dry-run`` is passed, print the plan and exit 0.
    4. Fail with exit code 3 (``ExitCode.FILESYSTEM_CONFLICT``) if any
       collision is detected and ``--overwrite`` was not passed.
    5. Stage the files (render into a temporary directory, validate
       the staged output), then commit the staged files to the
       destination.
    6. Generate the project manifest (``.forge/manifest.json``) and
       validate it against the manifest schema.
    7. Render a summary (destination, file count, next steps).
    8. Exit 0 on success, or the appropriate non-zero code on failure.

    Exit code contract:

    - 0 — Project created successfully (or dry-run completed).
    - 1 — User error (missing required variable, invalid input).
    - 2 — Blueprint error (not found, invalid, incompatible).
    - 3 — Filesystem conflict (collision, permission denied).
    - 5 — Internal error (unexpected exception).

    The ``--json`` flag will be added in the JSON output task, not
    here. This stub does not declare it yet, so the flag's absence is
    not a breaking change when it lands.
    """
    # TODO: implement the generation pipeline.
    # See the blueprint loader, variable resolver, generation engine,
    # and manifest tasks for the real implementation.
    #
    # The stub prints a message that includes every argument and
    # option the user passed. This makes manual verification of the
    # CLI contract trivial: if you see the values you passed, the
    # parsing works; if you do not, the parsing is broken.
    typer.echo(
        f"TODO: create {blueprint!r} as {name!r} in {output} "
        f"(no_interactive={no_interactive}, dry_run={dry_run}, "
        f"overwrite={overwrite})"
    )
