# 0003. Use a file-based command registry

- **Status:** Accepted
- **Date:** 2026-11-07
- **Deciders:** Thapelo Magqazana
- **Consulted:** —
- **Informed:** Forge CLI contributors

## Context

Forge CLI has six commands today (`list`, `create`, `validate`, `verify`,
`doctor`, `ping`) and will have many more as the project grows. The
roadmap names `inspect`, `drift`, `update`, `workspace`, `plugin`,
`policy`, and `version` as future commands. Every one of them needs to
be registered with the root Typer application, and every one of them
needs to appear in `forge --help`.

Before this decision, registration was a hard-coded list in
`src/forge/cli/main.py::create_app()`:

```python
from forge.cli.commands import create, doctor, validate, verify
from forge.cli.commands import list as list_cmd

app.add_typer(list_cmd.app, name="list")
app.add_typer(create.app, name="create")
app.add_typer(validate.app, name="validate")
app.add_typer(verify.app, name="verify")
app.add_typer(doctor.app, name="doctor")

@app.command(name="ping", help="Print 'pong' to confirm the CLI is working.")
def ping() -> None:
    typer.echo("pong")
```

This has three problems:

1. **Adding a command requires editing a central file.** Every new
   command module means a new import and a new `add_typer` call in
   `main.py`. The central file becomes a bottleneck for every
   contributor, and merge conflicts are guaranteed when multiple
   contributors add commands in parallel.

2. **The entry point knows about every command.** `main.py` is the CLI
   entry point, not a command inventory. Coupling it to the command
   list means it must be re-read and re-tested every time a command is
   added. It also makes `main.py` grow without bound — the file
   becomes longer with every new command, even though it does not do
   anything new.

3. **The registry is closed.** The Open/Closed Principle says a system
   should be open for extension and closed for modification. The
   explicit list is the opposite: every extension requires modifying
   the closed file. A contributor adding the tenth command does the
   same edit as the contributor who added the second — but the file
   is now ten times larger, and the risk of a merge conflict is ten
   times higher.

The constraints on the solution are:

1. **Open/Closed.** Adding a command must require adding a file, not
   editing a central list.
2. **Testable.** The mechanism must be exercisable in tests without
   spawning subprocesses or reinstalling the package.
3. **Graceful.** A malformed command module must not crash
   `forge --help`. The user must always get a working CLI, even if
   one command module is broken.
4. **Strict when it matters.** CI must catch malformed command modules
   *before* they reach users. The mechanism must support a strict
   mode that turns skips into errors.
5. **Deterministic.** The order of commands in `forge --help` must be
   stable across runs and platforms.

The question, then, is not *whether* to make command registration
modular. The question is *how*.

## Decision

We will use a **file-based command registry**, implemented in
`forge.cli.registry.register_commands()`.

### The command module contract

A *command module* is a Python module inside `forge.cli.commands` that
exposes a module-level attribute named `app` whose value is a
`typer.Typer` instance. Nothing else is required.

The contract is documented in `forge.cli.commands.__init__`, because
that is where new command authors will look. It is enforced in
`forge.cli.registry`, because that is where the registry needs to know
what to do with a module that violates it.

### The registry

`register_commands(root_app)` does four things:

1. **Discover** every non-private module in `forge.cli.commands` using
   `pkgutil.iter_modules`. The list is sorted for determinism.
2. **Import** each module via `importlib.import_module`. If a module
   fails to import, log a warning and skip it (or raise, under
   `strict=True`).
3. **Validate** the contract: the module must expose an `app`
   attribute of type `typer.Typer`. If not, log a warning and skip (or
   raise, under `strict=True`).
4. **Register** the app on the root app under the module's command
   name, derived from `app.info.name` (which Typer defaults to the
   module name if not explicitly set). If two modules would register
   the same command name, skip the second (or raise, under
   `strict=True`).

### Graceful by default, strict when asked

The registry accepts a `strict` parameter:

- **`strict=False`** (the default, used by `forge --help` and every
  user-facing invocation): a malformed module is logged and skipped.
  `forge --help` never crashes because of one bad command.
- **`strict=True`** (used by tests and CI): a malformed module raises
  `RuntimeError`. The test suite catches contract violations before
  they reach users.

The `on_skip` callback fires once per skipped module, so tests can
assert on skips without parsing log output.

### The `ping` command as proof

The `ping` command — previously hard-coded in `main.py` — was moved to
`forge.cli.commands.ping`. It is now discovered by the same mechanism
as every other command. If `forge ping` works, the registry works. The
`ping` command is the smallest possible proof of the largest possible
claim.

## Consequences

### Positive

- **Open/Closed.** Adding a command means adding a file. The registry
  is never edited. Merge conflicts on a central command list are
  impossible.

- **`main.py` is decoupled.** The entry point no longer knows which
  commands exist. It calls `register_commands(app)` and lets the
  registry do its job. The file does not grow with every new command.

- **Contract violations are caught in CI, not in production.** The
  test suite runs `register_commands(strict=True)`, so a command
  module that forgets to expose `app` fails CI before a user sees it.

- **Deterministic output.** Discovery is sorted, so `forge --help`
  lists commands in a stable order across runs and platforms.

- **Testable in isolation.** The registry has its own test file
  (`tests/unit/cli/test_registry.py`) with 26 tests covering
  discovery, registration, graceful failure, strict mode, duplicate
  detection, and the `on_skip` callback. The registry does not depend
  on which commands currently exist.

- **Consistent command modules.** Every command module in the project
  follows the same structure (module docstring, module-level `app`,
  `@app.command` or `@app.callback`). This consistency makes the
  codebase easier to read and extend.

- **Frozen-binary path is clear.** When the frozen-binary fallback
  lands (see follow-up actions), it will replace `pkgutil.iter_modules`
  with a static list. The contract does not change; only the discovery
  mechanism does.

### Negative

- **The contract is invisible at the point of use.** A new command
  author must read `forge.cli.commands.__init__` to learn that they
  must expose `app`. This is documented in the package docstring, in
  `CONTRIBUTING.md`, and in this ADR, but it is still a convention
  that must be learned.

- **Import errors are less obvious.** In the explicit list, a broken
  command module caused an immediate `ImportError` at startup. In the
  registry, a broken module is logged and skipped (unless
  `strict=True`). This is intentional — `forge --help` must not crash
  — but it means a developer running `forge list` might not see their
  new command until they check the logs or run the test suite.

- **Discovery depends on `pkgutil`.** The registry uses
  `pkgutil.iter_modules` to scan the package. This works for normal
  packages, but not for frozen binaries (PyInstaller, cx_Freeze) that
  flatten modules. For the frozen-binary case, a fallback static list
  will be needed. This is a known limitation, documented in the
  follow-up actions.

- **The command name is derived, not declared.** The command name is
  `app.info.name` (which Typer defaults to the module name if not
  explicitly set). A contributor who names their module `my_command.py`
  and their app `my-command` will register a command named `my-command`,
  which is correct — but a contributor who names their module
  `my_command.py` and forgets to set `app.info.name` will register
  `my_command`, which does not match the project's hyphenated
  convention. This is a documentation issue, not a runtime bug, but it
  is a real risk.

- **Two files must agree.** The registry's `_get_typer_app()` checks
  `isinstance(candidate, typer.Typer)`. If Typer ever changes its
  class hierarchy (unlikely, but possible), the check could break.
  This is a low-probability risk but worth noting.

### Follow-up actions

- [x] Implement `forge.cli.registry.register_commands()` with
      discovery, validation, registration, and duplicate detection.
- [x] Document the command module contract in
      `forge.cli.commands.__init__`.
- [x] Move `ping` from `main.py` to `forge.cli.commands.ping` as the
      proof of the registry.
- [x] Write `tests/unit/cli/test_registry.py` with tests for
      discovery, registration, graceful failure, strict mode,
      duplicate detection, and the `on_skip` callback.
- [x] Update `tests/unit/cli/test_main.py` to reflect the registry
      (the help test now points at the command modules, not the
      removed `add_typer` calls).
- [ ] Add a frozen-binary fallback: if `pkgutil.iter_modules` returns
      no modules (e.g., in a PyInstaller bundle), fall back to a
      static list of known commands. This will be needed when the
      first frozen binary is produced.
- [ ] Add a CI check that runs `register_commands(strict=True)` on
      the built wheel, to catch any contract violations in packaged
      command modules.
- [ ] Document the command module contract in `CONTRIBUTING.md`
      under a new "Adding a Command" section.
- [ ] Add a `forge plugin` command (the future plugin system) that
      reuses the same registry, so third-party commands follow the
      same contract as built-in commands.

## Alternatives Considered

### Alternative 1: A hard-coded list in `main.py` (the status quo)

**Why rejected:** This is the status quo, and it is the problem. Every
new command requires editing a central file, which is a bottleneck
and a merge-conflict magnet. The entry point grows without bound, and
the Open/Closed Principle is violated on every addition.

### Alternative 2: A decorator that registers commands at import time

```python
# In main.py
from forge.cli.registry import register

@register
def list_command() -> None:
    ...
```

**Why rejected:** A decorator-based registry requires `main.py` to
import every command module so the decorator runs. That is the same
coupling as the explicit list, just disguised. It also makes command
modules importable only through `main.py`, which prevents testing
them in isolation. The decorator hides the command's existence from
`forge --help` until the module is imported, which is the opposite of
what a help screen is for.

### Alternative 3: A decorator that writes to a global registry, with `pkgutil` discovery

```python
# In each command module
from forge.cli.registry import register

@register
def list_command() -> None:
    ...
```

**Why rejected:** This is the "magic" version of Alternative 2. It
requires a global registry that the decorator writes to, and it
requires importing every module to trigger registration. The order of
imports becomes the order of registration, which is non-deterministic
in Python (dict ordering is guaranteed in modern Python, but import
order is not). The registry is a hidden global, which is harder to
test than an explicit `register_commands(app)` function. It also
conflicts with the project's principle of "no hidden state".

### Alternative 4: Python entry points (`[project.entry-points]`)

**Why rejected:** Entry points require the command module to be
installed as a separate package, or at least to be discoverable via
`importlib.metadata.entry_points`. For a single package like Forge CLI,
this is unnecessary indirection. It also requires a rebuild every time
a command is added, which is the opposite of the Open/Closed
Principle — adding a command in a source checkout would require
reinstalling the package. Entry points are the right answer for a
*plugin system* (where third-party packages register commands), not
for a first-party command list.

### Alternative 5: A plugin system (third-party command packages)

**Why rejected:** A plugin system is the right answer *eventually*,
when Forge supports third-party command extensions. It is the wrong
answer *now*, because it adds complexity (a plugin API, a permissions
model, a compatibility contract) that the current codebase does not
need. The file-based registry is the simplest thing that could work,
and it is designed to be compatible with a future plugin system
(commands are already modules with a `Typer` app, which is exactly
what a plugin would expose). The `forge plugin` command in the future
roadmap will reuse this registry, not replace it.

### Alternative 6: An `__init__.py` that imports every command module

```python
# In forge/cli/commands/__init__.py
from . import list, create, validate, verify, doctor, ping
```

**Why rejected:** This is Alternative 1 in disguise. It moves the
explicit list from `main.py` to `commands/__init__.py`, but it is
still a central file that must be edited every time a command is
added. It also imports every command module eagerly, which slows
startup and makes it harder to test individual modules. The registry
is *lazy*: it discovers modules without importing them, and imports
each one only when it is registered.

## References

- `pkgutil` documentation:
  https://docs.python.org/3/library/pkgutil.html
- Typer documentation on sub-applications:
  https://typer.tiangolo.com/tutorial/subcommands/
- Open/Closed Principle:
  https://en.wikipedia.org/wiki/Open%E2%80%93closed_principle
- ADR 0001 (Use Typer for the CLI layer), which established the
  `Typer`-based CLI that this registry plugs into.
- Implementation: `src/forge/cli/registry.py`
- Contract: `src/forge/cli/commands/__init__.py`
- Tests: `tests/unit/cli/test_registry.py`
