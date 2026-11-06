"""``forge list`` — list available blueprints.

This command is the first user-facing discovery command in Forge CLI.
It will eventually query the blueprint registry (both the bundled
official blueprints and any local custom blueprints) and render the
results as a table (human mode) or a JSON array (``--json``).

At the current stage it is a stub: it prints a TODO message. The
module's structure — the module-level ``app``, the explicit ``name=``
and ``help=`` on the ``Typer`` constructor, the ``@app.callback``
decorator — is final. The implementation of the body will be added in
the blueprint discovery task.
"""

from __future__ import annotations

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
# in ``forge --help`` and ``forge list --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="list",
    help="List available blueprints.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def list_blueprints() -> None:
    """List all discoverable blueprints.

    This is a stub. The real implementation will:

    1. Discover blueprints from ``forge.blueprints`` (bundled) and from
       the user's local ``.forge/blueprints/`` directory (custom).
    2. Render the results as a Rich table with columns ``ID``,
       ``Name``, ``Version``, ``Category``, and ``Status``.
    3. Support ``--json`` for machine-readable output.
    4. Sort by ID for deterministic output.

    The callback is invoked without a subcommand because ``forge list``
    is a single command, not a command group. If subcommands are added
    later (e.g., ``forge list official``, ``forge list local``), the
    ``invoke_without_command`` flag will let the bare ``forge list``
    still work.
    """
    # TODO: implement blueprint discovery and table rendering.
    # See the blueprint discovery task for the real implementation.
    typer.echo("TODO: list blueprints")
