"""``forge validate`` — validate a blueprint definition.

This command validates a blueprint against the JSON Schema and the
Pydantic models, without generating a project. It is the primary tool
for blueprint authors: they can run it against a local blueprint
before publishing it, and they get structured diagnostics with
file/line locations, messages, and hints.

At the current stage it is a stub: it prints a TODO message. The
module's structure — the module-level ``app``, the explicit ``name=``
and ``help=`` on the ``Typer`` constructor, the ``@app.command``
decorator, the ``path`` argument — is final. The implementation of
the body will be added in the blueprint loader task.
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
# in ``forge --help`` and ``forge validate --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="validate",
    help="Validate a blueprint definition.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.command()
def validate(
    path: Path = typer.Argument(
        ...,
        metavar="PATH",
        help=(
            "Path to a blueprint directory (containing blueprint.yaml) "
            "or to a blueprint.yaml file."
        ),
        # ``exists=True`` makes Typer validate that the path exists
        # before the command body runs. If the path does not exist,
        # Typer prints "Invalid value for 'PATH': Path '...' does not
        # exist." and exits with code 2 (usage error). This is the
        # correct behaviour: a missing path is a user error, not a
        # blueprint error, and it should be caught before the
        # validation pipeline starts.
        exists=True,
        # ``file_okay=True`` and ``dir_okay=True`` allow both a file
        # path (``./blueprint.yaml``) and a directory path
        # (``./my-blueprint/``). The command body will resolve which
        # one was passed and locate the blueprint accordingly.
        file_okay=True,
        dir_okay=True,
        # ``readable=True`` ensures the path can be read before the
        # command body runs. This catches permission errors early,
        # with a clear message, instead of deep inside the loader.
        readable=True,
        # ``resolve_path=True`` makes Typer resolve the path to an
        # absolute path before passing it to the command body. This
        # avoids ambiguity when the command changes the working
        # directory (it does not, but future refactors might), and it
        # produces clearer error messages.
        resolve_path=True,
    ),
) -> None:
    """Validate a blueprint without generating a project.

    This is a stub. The real implementation will:

    1. Resolve the path to a blueprint.yaml file (either the path
       itself, or ``<path>/blueprint.yaml`` if the path is a
       directory).
    2. Load the blueprint using ``forge.blueprints.loader``, which
       parses the YAML, validates it against the JSON Schema, and
       constructs the Pydantic models.
    3. Collect all validation errors (not just the first one), each
       with a path (e.g., ``blueprint.id``), a message, and a hint.
    4. Render the errors as a Rich table on stdout (human mode) or as
       a JSON object on stdout (``--json`` mode).
    5. Exit with code 0 if the blueprint is valid, or code 2
       (``ExitCode.BLUEPRINT_ERROR``) if it is not.

    The ``--json`` flag will be added in the JSON output task, not
    here. This stub does not declare it yet, so the flag's absence is
    not a breaking change when it lands.
    """
    # TODO: implement blueprint validation.
    # See the blueprint loader and validator tasks for the real
    # implementation.
    typer.echo(f"TODO: validate {path}")
