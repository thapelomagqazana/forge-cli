"""``forge ping`` — print 'pong' to confirm the CLI is working.

This command exists for three reasons:

1. **Health check.** It is the fastest way to verify that a fresh
   install works end-to-end. If ``forge ping`` fails, the problem is
   installation, not the command the user was trying to run. This is
   the first thing to check when a bug report says "forge does not
   work."

2. **Reference implementation.** It is the simplest possible command
   module — the smallest code that satisfies the command module
   contract. New contributors can read this file to see exactly what
   a command module looks like before reading the more complex ones
   (``create.py``, ``verify.py``, ``doctor.py``).

3. **Proof of the registry.** This command was previously hard-coded
   in ``forge.cli.main.create_app``. It was moved here, into the
   commands package, so that it is discovered by the same
   ``pkgutil``-based mechanism as every other command. If
   ``forge ping`` works, the registry works. That is the whole proof.
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
# in ``forge --help`` and ``forge ping --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="ping",
    help="Print 'pong' to confirm the CLI is working.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def ping() -> None:
    """Print 'pong' to confirm the CLI is working.

    This command takes no arguments, does no I/O beyond ``print``, and
    always exits 0. It is the simplest possible Typer command.

    The callback is invoked without a subcommand because ``forge ping``
    is a single command, not a command group. There is no
    ``forge ping <subcommand>``. Using ``@app.callback`` (rather than
    ``@app.command``) is the pattern for commands that have no
    subcommands and take no positional arguments — it is the same
    pattern used by ``forge list`` and ``forge doctor``.
    """
    typer.echo("pong")
