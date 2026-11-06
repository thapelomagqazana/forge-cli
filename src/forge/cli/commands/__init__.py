"""Forge CLI command modules.

Every module in this package is a *command module*. The registry
(``forge.cli.registry``) discovers every module here and registers its
``app`` attribute with the root Typer application.

The contract
------------
A command module MUST:

1. Live in this package (``forge.cli.commands``).
2. Expose a module-level attribute named ``app`` whose value is a
   ``typer.Typer`` instance.

A command module SHOULD:

3. Construct its ``app`` with an explicit ``name`` and ``help`` so the
   root ``forge --help`` output is readable::

       app = typer.Typer(
           name="list",
           help="List available blueprints.",
       )

4. Keep the app's *commands* in the same module. A module may define
   one or more commands on its ``app``; they will all be reachable as
   ``forge <name> <subcommand>``.

5. Document the module's purpose in a module docstring, so the next
   contributor knows what it does without reading the code.

A command module MUST NOT:

6. Be named with a leading underscore (``_helpers.py``). Modules whose
   names start with ``_`` are skipped by the registry — use them for
   shared helpers, not commands.

7. Expose an ``app`` that is not a ``typer.Typer``. If the registry
   finds a non-Typer ``app``, it logs a warning and skips the module
   (or raises, under ``strict=True``).

8. Duplicate a command name that another module already registered.
   The registry detects duplicates and skips the later one (or raises,
   under ``strict=True``).

Adding a new command
--------------------
1. Create a new module here: ``src/forge/cli/commands/<name>.py``.
2. Define a ``typer.Typer`` app with an explicit name and help.
3. Define one or more commands on that app.
4. Run ``make test`` (strict mode) to verify the contract is met.
5. Run ``make check`` to verify the full gate.

The command is now reachable as ``forge <name>``. No other file
needs to change. That is the Open/Closed Principle in action: the
registry is open for extension (add a file), closed for modification
(never edit the registry to add a command).

Why the contract lives here
---------------------------
The contract is documented here, not in ``registry.py``, because this
is where new command authors will look. A registry that documents its
own contract is a registry that new contributors do not read. A
package that documents the contract it expects is a package that new
contributors cannot get wrong by accident.
"""

from __future__ import annotations

__all__: list[str] = []
