# Adding a Command

This guide walks you through adding a new command to Forge CLI. By
the end, you will have a working command that:

- Is discoverable via `forge --help`.
- Has a `--help` output that documents its arguments and options.
- Uses the correct output helpers (so its messages go to the right
  stream).
- Raises the correct exception types (so its errors exit with the
  right code).
- Has tests that pass under `make check`.

The guide takes about 15 minutes to complete. If you are only
looking for a *reference* of the command module contract, see
[`src/forge/cli/commands/__init__.py`](../../src/forge/cli/commands/__init__.py).

---

## Before you start

You will need:

- A working development environment. Run `make install` if you
  have not already. See
  [`development-setup.md`](development-setup.md) for details.
- A clean working tree. Run `git status` and commit or stash any
  in-progress changes.
- A name for your command. It should be a single lowercase word,
  hyphens allowed, no underscores. Examples: `status`, `inspect`,
  `blueprint-check`.

This guide uses `status` as the running example. The command will
print a one-line summary of the installed Forge version and the
current directory. The example is deliberately trivial — the *point*
is the structure, not the logic.

---

## Step 1: Create the command module

Every command is a module in `src/forge/cli/commands/`. The module
name is the command name (e.g., `status.py` for `forge status`).

```bash
touch src/forge/cli/commands/status.py
```

The module must conform to the command module contract: it exposes a
module-level attribute named `app` whose value is a `typer.Typer`
instance.

Open `src/forge/cli/commands/status.py` and add:

```python
"""``forge status`` — show the current Forge status."""
from __future__ import annotations

import typer

app = typer.Typer(
    name="status",
    help="Show the current Forge status.",
)


@app.callback(invoke_without_command=True)
def status() -> None:
    """Show the current Forge status.

    Prints the installed Forge version and the current working
    directory. The output is a single line, suitable for piping.
    """
    typer.echo("status")
```

**Why `@app.callback(invoke_without_command=True)` instead of
`@app.command()`?**

Use `@app.callback` when the command has no subcommands and takes no
positional arguments. The callback runs when the user types `forge
status` (with no further arguments). Use `@app.command()` when the
command *is* the subcommand (e.g., `forge create` has one
subcommand, `create`, which takes a `BLUEPRINT` argument).

If you are unsure, look at the existing command modules:

- `ping.py` uses `@app.callback` (no arguments).
- `create.py` uses `@app.command` (takes a `BLUEPRINT` argument).

---

## Step 2: Verify the command is registered

The registry discovers `status.py` automatically. There is no
central list to edit. Verify with:

```bash
uv run forge --help
```

You should see `status` listed under `Commands`, with the
description `"Show the current Forge status."`.

Then verify the command runs:

```bash
uv run forge status
```

You should see:

```
status
```

And verify the command's own help:

```bash
uv run forge status --help
```

You should see the callback docstring and the command name.

**If you do not see `status` in `forge --help`**, the most common
cause is a missing `app` attribute or an `app` that is not a
`typer.Typer`. Run the registry's strict check to see the exact
diagnostic:

```bash
uv run python -c "
from forge.cli.registry import register_commands
import typer
register_commands(typer.Typer(), strict=True)
"
```

If `status.py` violates the contract, this command will raise a
`RuntimeError` with a message naming the violation.

---

## Step 3: Use the output helpers

The command's `typer.echo("status")` works, but it bypasses the
CLI's voice. Every user-facing message should go through one of
`forge.cli.ui`'s helpers, so the format is consistent across
commands.

Replace the `typer.echo` call with `print_info` (or the appropriate
helper for your message):

```python
"""``forge status`` — show the current Forge status."""
from __future__ import annotations

from pathlib import Path

import typer

from forge.cli.ui import print_info
from forge.core.version import get_version

app = typer.Typer(
    name="status",
    help="Show the current Forge status.",
)


@app.callback(invoke_without_command=True)
def status() -> None:
    """Show the current Forge status.

    Prints the installed Forge version and the current working
    directory. The output is a single line, suitable for piping.
    """
    cwd = Path.cwd()
    print_info(f"Forge {get_version()} at {cwd}")
```

**Which helper should I use?**

| Helper | When to use |
| :--- | :--- |
| `print_success` | The command completed a requested action (e.g., a project was created). |
| `print_info` | The command is producing its *product* (the result the user asked for). |
| `print_warning` | The command completed but something is suboptimal (e.g., Docker is not available). |
| `print_error` | The command failed; an error message. **Use this only if you are raising a `ForgeError`** — the classifier will render it for you. |
| `print_hint` | A remediation hint that accompanies an error. **The classifier renders it for you; do not call it directly from a command.** |
| `print_cancelled` | The user cancelled. **The classifier renders it for you; do not call it directly from a command.** |

For a command that produces output (like `status`), `print_info` is
usually the right choice. For a command that performs an action,
`print_success` is usually the right choice. Errors, hints, and
cancellations are rendered by the classifier — commands do not call
those helpers directly.

Run the command again:

```bash
uv run forge status
```

You should see:

```
Forge 0.1.0a0 at /home/user/projects/forge-cli
```

---

## Step 4: Add arguments and options (if needed)

Most commands take arguments or options. Add them with Typer's
`Annotated` style:

```python
"""``forge status`` — show the current Forge status."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from forge.cli.ui import print_info
from forge.core.version import get_version

app = typer.Typer(
    name="status",
    help="Show the current Forge status.",
)


@app.callback(invoke_without_command=True)
def status(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="Directory to report status for. Defaults to the current directory.",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = Path("."),
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show additional details (Python version, platform).",
        ),
    ] = False,
) -> None:
    """Show the current Forge status.

    Prints the installed Forge version and the directory being
    inspected. With ``--verbose``, also prints the Python version
    and platform.
    """
    print_info(f"Forge {get_version()} at {path}")

    if verbose:
        import platform
        import sys

        print_info(f"Python {sys.version.split()[0]} on {platform.system()}")
```

Run the command with and without options:

```bash
uv run forge status
uv run forge status --path /tmp
uv run forge status --verbose
uv run forge status --help
```

**Which style should I use: `Annotated` or the older `x: str =
typer.Option(...)`?**

Use `Annotated`. It is the modern style, and it composes better with
type hints. The older style (`x: str = typer.Option(...)`) still
works but is discouraged.

**Which arguments should be `typer.Argument` and which `typer.Option`?**

- Use `typer.Argument` for the command's *subject* — the thing the
  command acts on. `forge create python.fastapi.api` — the blueprint
  is the subject, so it is an argument.
- Use `typer.Option` for *modifiers* — flags that change how the
  command runs. `forge status --path /tmp` — the path is a modifier,
  so it is an option.

A common mistake is making everything an option. If the user types
`forge status /tmp`, the path is an argument. If they type `forge
status --path /tmp`, it is an option. The choice is a UX decision:
arguments are positional and usually required; options are named and
usually optional.

---

## Step 5: Raise the right exceptions

If your command can fail in a way the user can fix (invalid input,
missing file, etc.), raise a `ForgeError` subclass. The classifier
will render it with the right message and exit with the right code —
do not `print_error` and `sys.exit` yourself.

```python
"""``forge status`` — show the current Forge status."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from forge.cli.ui import print_info
from forge.core.exceptions import UserInputError
from forge.core.version import get_version

app = typer.Typer(
    name="status",
    help="Show the current Forge status.",
)


@app.callback(invoke_without_command=True)
def status(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="Directory to report status for. Defaults to the current directory.",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = Path("."),
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show additional details (Python version, platform).",
        ),
    ] = False,
) -> None:
    """Show the current Forge status.

    Prints the installed Forge version and the directory being
    inspected. With ``--verbose``, also prints the Python version
    and platform.
    """
    if not path.is_dir():
        raise UserInputError(
            f"{path} is not a directory.",
            hint="Pass a path to a directory, or omit --path to use the current directory.",
        )

    print_info(f"Forge {get_version()} at {path}")

    if verbose:
        import platform
        import sys

        print_info(f"Python {sys.version.split()[0]} on {platform.system()}")
```

**Which exception should I raise?**

Look at `src/forge/core/exceptions.py`. The four subclasses each
correspond to a category of failure:

| Exception | When to raise | Exit code |
| :--- | :--- | :--- |
| `UserInputError` | The user passed invalid input (a path that does not exist, a flag that conflicts with another, a value that does not match a pattern). | 1 |
| `BlueprintError` | A blueprint is missing, malformed, or incompatible. | 2 |
| `FilesystemConflictError` | A target would be overwritten, or a path escapes the permitted root. | 3 |
| `VerificationError` | One or more verification checks did not pass. | 4 |

For anything else, raise a built-in exception (`RuntimeError`,
`ValueError`, etc.) and let the classifier handle it. If you find
yourself wanting a *fifth* category, read
[`../architecture/error-handling.md`](../architecture/error-handling.md)
first — it explains why the four categories are enough, and when a
new one is justified.

**Should I include a hint?**

Yes, whenever you can. A hint turns a dead end into a signpost. A
hint is a one-line remediation suggestion: *what to do next*, not
*what went wrong* (the message already says that). Good hints start
with a verb: "Pass --name my-project", "Run `forge list` to see
available blueprints", "Check that Docker is running".

See [`adding-an-exception.md`](adding-an-exception.md) for more
guidance.

---

## Step 6: Add tests

Every new command needs tests. The tests live in
`tests/unit/cli/test_main.py` (for user-facing behavior) or a new
file (for complex commands).

The minimum test suite for a command:

1. **The command appears in `forge --help`.**
2. **The command runs with no arguments and exits 0.**
3. **The command's `--help` works.**
4. **Each option behaves as documented.**
5. **Each error path raises the right exception and exits with the
   right code.**

Here is a minimal test suite for `status`:

```python
"""Tests for ``forge status``."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner


@pytest.mark.unit()
class TestStatus:
    """Tests for the ``forge status`` command."""

    def test_appears_in_help(
        self, cli_runner: CliRunner, forge_app
    ) -> None:
        """The command is listed in `forge --help`."""
        result = cli_runner.invoke(forge_app, ["--help"])
        assert result.exit_code == 0, result.output
        assert "status" in result.output

    def test_runs_with_no_arguments(
        self, cli_runner: CliRunner, forge_app
    ) -> None:
        """`forge status` exits 0 and prints the version."""
        result = cli_runner.invoke(forge_app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Forge" in result.output

    def test_help_documents_options(
        self, cli_runner: CliRunner, forge_app
    ) -> None:
        """`forge status --help` documents --path and --verbose."""
        result = cli_runner.invoke(forge_app, ["status", "--help"])
        assert result.exit_code == 0, result.output
        assert "--path" in result.output
        assert "--verbose" in result.output

    def test_path_option(
        self, cli_runner: CliRunner, forge_app, tmp_path
    ) -> None:
        """`forge status --path <dir>` prints the given path."""
        result = cli_runner.invoke(
            forge_app, ["status", "--path", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert str(tmp_path) in result.output

    def test_verbose_option(
        self, cli_runner: CliRunner, forge_app
    ) -> None:
        """`forge status --verbose` prints the Python version."""
        result = cli_runner.invoke(forge_app, ["status", "--verbose"])
        assert result.exit_code == 0, result.output
        assert "Python" in result.output

    def test_invalid_path_exits_with_user_error(
        self, cli_runner: CliRunner, forge_app
    ) -> None:
        """An invalid path raises UserInputError, exits 1."""
        result = cli_runner.invoke(
            forge_app, ["status", "--path", "/nonexistent"]
        )
        assert result.exit_code == 1, result.output
```

Add this to a new file, `tests/unit/cli/commands/test_status.py`, or
append it to `tests/unit/cli/test_main.py`. Prefer the new file if
the command has more than a few tests — the file-per-module
convention keeps `test_main.py` focused on the entry point.

Run the tests:

```bash
make test
```

If any test fails, the failure message will tell you which assertion
failed and what the actual output was. Fix the code (not the test)
and re-run.

---

## Step 7: Update the documentation

If the command is user-facing (i.e., it will be documented for
users, not just contributors), update the command reference in
`docs/reference/commands/`. This is the public documentation — it
will be read by users who are trying to use the command.

If the command is internal (only used by Forge's own tests or by
future tasks), a brief note in the module docstring is enough.

Every command should have:

- A one-line summary in `forge --help`.
- A `--help` docstring on the callback that explains what the
  command does and what its options are.
- A `docs/reference/commands/status.md` page if the command is
  user-facing.

The `--help` docstring and the reference page should be consistent.
If you update one, update the other.

---

## Step 8: Run the full gate

```bash
make check
```

This runs lint, format, type, test, security, and ADR check. Every
one must pass. If any fails, the output tells you which file, which
line, and what to do about it.

The most common failures for a new command:

| Failure | Cause | Fix |
| :--- | :--- | :--- |
| `ruff check` fails | Missing import, unused variable, style violation | Fix the code, then run `make fix` to auto-fix formatting. |
| `mypy` fails | Missing type annotation, wrong type, missing `__all__` entry | Add the annotation, fix the type, add the `__all__` entry. |
| `pytest` fails | Test does not match the behavior | Fix the code (or the test, if the test is wrong — but usually the code). |
| Coverage < 90% | The new module or its new branches are not tested | Add tests for the new code paths. |
| `adr-check` fails | You forgot to run `make adr-index` | Run `make adr-index`, then re-stage. |

---

## Step 9: Commit

```bash
git add src/forge/cli/commands/status.py \
        tests/unit/cli/commands/test_status.py \
        docs/reference/commands/status.md
git commit -s -m "feat(cli): add the forge status command

Add a new command that reports the installed Forge version and the
current directory, with options for the path to inspect and for
verbose output.

The command is discoverable via 'forge --help', has its own
'--help' output, uses ui.print_info for its product output, and
raises UserInputError for an invalid path (exit 1).

Tests cover the happy path, the --help output, both options, and
the error path."
```

Once committed, your command is part of Forge CLI. The registry
discovered it without a central edit. `forge --help` lists it. Its
`--help` documents it. Its errors are classified by the classifier.
Its messages go to the right stream. That is what "adding a command"
means in this codebase — a file, a test, a commit.

---

## Checklist

Before you open a PR, confirm:

- [ ] The module is at `src/forge/cli/commands/<name>.py`.
- [ ] It exposes a module-level `app: typer.Typer`.
- [ ] The `app` has an explicit `name=` and `help=`.
- [ ] The callback has a docstring that explains what the command
      does and what its options are.
- [ ] It uses `ui.print_*` helpers, not `typer.echo` or `print`.
- [ ] It raises `ForgeError` subclasses for user errors, not
      `print_error` + `sys.exit`.
- [ ] It does not `sys.exit` — the classifier handles exit codes.
- [ ] It has tests for the happy path, the `--help` output, each
      option, and each error path.
- [ ] `make check` passes.
- [ ] The `--help` docstring and any reference page agree.

---

## Further reading

- [`../architecture/cli-architecture.md`](../architecture/cli-architecture.md)
  — the CLI layer in detail
- [`../architecture/error-handling.md`](../architecture/error-handling.md)
  — the classifier's contract
- [`adding-an-exception.md`](adding-an-exception.md) — how to add a
  new `ForgeError` subclass
- [`../reference/exit-codes.md`](../reference/exit-codes.md) — the
  public exit-code contract
- [`../reference/streams.md`](../reference/streams.md) — the
  stdout/stderr contract
- [`src/forge/cli/commands/__init__.py`](../../src/forge/cli/commands/__init__.py)
  — the command module contract, in the place new contributors look
- [`src/forge/cli/commands/ping.py`](../../src/forge/cli/commands/ping.py)
  — the smallest possible command module
- [`src/forge/cli/commands/create.py`](../../src/forge/cli/commands/create.py)
  — the most complex command module
