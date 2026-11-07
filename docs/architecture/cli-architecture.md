# CLI Architecture

This document describes the `forge.cli` package: its modules, their
contracts, and their interactions. It is the detailed companion to
[overview.md](overview.md), which covers the whole codebase.

If you are adding a command, the short version is: add a file to
`cli/commands/`. The long version is in
[../guides/adding-a-command.md](../guides/adding-a-command.md).

If you are changing how errors are rendered, read
[error-handling.md](error-handling.md) first.

---

## The six modules

`forge.cli` is composed of six modules, each with a single
responsibility:

| Module | Responsibility | Lines | Tests |
| :--- | :--- | :--- | :--- |
| `main.py` | The entry point. Creates the app, delegates registration and error handling, exits. | ~200 | `test_main.py` |
| `registry.py` | Discovers command modules, validates their contract, registers them. | ~200 | `test_registry.py` |
| `ui.py` | The CLI's voice. Six output helpers. | ~200 | `test_ui.py` |
| `errors.py` | The exception classifier. Dispatches on exception type. | ~200 | `test_errors.py` |
| `__main__.py` | The `python -m forge.cli` fallback. | ~20 | `test_main.py` |
| `__init__.py` | Intentionally empty. | ~30 (docstring only) | n/a |

The six commands live in `cli/commands/`. Each is a separate module
that exposes a `typer.Typer` as `app`.

The `__init__.py`'s emptiness is a *feature*, not an oversight. See
[The empty `__init__.py`](#the-empty-initpy) below.

---

## The flow of a command

### Happy path: `forge ping`

```
1. Console script (`~/.venv/bin/forge`)
   └── calls `forge.cli.main:main()`

2. `main()`
   ├── creates the app (`create_app()`)
   │   ├── instantiates `typer.Typer(...)`
   │   ├── registers the root callback (`--version`, `--debug`)
   │   └── calls `register_commands(app)`
   │       ├── discovers modules in `forge.cli.commands`
   │       ├── validates each module's `app: typer.Typer`
   │       └── mounts each on the root app
   ├── resolves `--debug` from argv
   ├── calls `app(args=argv, standalone_mode=False)`
   │   └── Typer dispatches to the `ping` command
   │       └── `ping()` prints `pong`
   └── raises `SystemExit(ExitCode.SUCCESS)`

3. Console script calls `sys.exit(main())`
   └── exits 0
```

### Failure path: `forge nonsense-command`

```
1. Console script
   └── calls `forge.cli.main:main()`

2. `main()`
   ├── creates the app
   ├── calls `app(args=argv, standalone_mode=False)`
   │   └── Typer raises `click.UsageError("No such command ...")`
   ├── `except BaseException as exc:` catches it
   │   └── calls `errors.handle_exception(exc, debug=False)`
   │       ├── classifies: is it a `click.ClickException`? Yes.
   │       ├── renders: `ui.print_error(exc.format_message())`
   │       └── raises `SystemExit(2)`
   └── exits 2

3. Console script's `sys.exit(main())` exits 2.
   User sees: `✗ No such command 'nonsense-command'.`
```

The key insight: **`main()` is a dispatcher, not a renderer**. It
creates the app, delegates registration, delegates error handling,
and exits. The *content* of each operation belongs to a specialist
module.

---

## `main.py` — the entry point

**File:** `src/forge/cli/main.py`

**Responsibility:** create the app, delegate registration and error
handling, exit.

### Public API

| Symbol | Signature | Purpose |
| :--- | :--- | :--- |
| `main` | `(argv: list[str] \| None = None) -> NoReturn` | The process entry point. Declared `-> NoReturn` because every path exits. |
| `create_app` | `() -> typer.Typer` | The app factory. Re-creatable for tests and for future multi-app scenarios. |

### Private helpers

| Symbol | Purpose |
| :--- | :--- |
| `_version_callback` | The `--version` callback. Calls `ui.print_info(f"forge, version {get_version()}")` and exits 0. |
| `_resolve_debug_flag` | Returns `True` if `--debug` is in argv. A literal `in` check — not a full parser — because the root callback already parses `--debug`, and this helper is only the *fallback* for the case where the callback did not run. |

### What it does *not* do

- It does **not** print anything except the version. All other
  output goes through `ui.py`.
- It does **not** render errors. All error rendering goes through
  `errors.py`.
- It does **not** know which commands exist. Command discovery
  belongs to `registry.py`.
- It does **not** own a console. The console lives in `ui.py`.

### Dependencies

```
main.py ──> registry.py (register_commands)
       ──> errors.py (handle_exception)
       ──> ui.py (print_info for --version)
       ──> core.version (get_version)
       ──> core.exceptions (ExitCode)
```

### Why it is thin

`main.py` is a dispatcher, not a renderer. This is the *factory
pattern*: the entry point creates the app, wires the dependencies,
and hands off. Every subsequent operation belongs to a specialist.
The benefit is testability — `create_app()` can be invoked without
launching a subprocess, and `_resolve_debug_flag` can be tested as a
pure function.

---

## `registry.py` — the command discovery

**File:** `src/forge/cli/registry.py`

**Responsibility:** find every command module, validate its contract,
register it on the app.

### Public API

| Symbol | Signature | Purpose |
| :--- | :--- | :--- |
| `register_commands` | `(root_app, *, package=COMMANDS_PACKAGE, strict=False, on_skip=None) -> list[RegisteredCommand \| SkippedCommand]` | The public entry point. Discovers, validates, registers. |
| `discover_command_modules` | `(package=COMMANDS_PACKAGE) -> list[str]` | The discovery helper. Returns fully-qualified module names, sorted. |
| `RegisteredCommand` | dataclass | A successfully registered module. |
| `SkippedCommand` | dataclass | A module that was found but not registered, with the reason. |
| `APP_ATTRIBUTE` | `"app"` | The attribute name a command module must expose. |
| `COMMANDS_PACKAGE` | `"forge.cli.commands"` | The package to scan. |

### The command module contract

A *command module* is a Python module inside `forge.cli.commands`
that exposes a module-level attribute named `app` whose value is a
`typer.Typer` instance. Nothing else is required.

The contract is documented in `cli/commands/__init__.py`. It is
enforced in `registry.py`. The split is deliberate: the contract is
documented *where new command authors look*, and enforced *where
the registry needs to act on it*.

### Design principles

**Open/Closed.** Adding a command means adding a file. The registry
is never edited. Merge conflicts on a central command list are
impossible.

**Graceful by default, strict when asked.** `strict=False` (the
default) logs a warning and skips a malformed module, so
`forge --help` never crashes. `strict=True` raises, so CI catches
contract violations before they reach users.

**Deterministic.** Modules are sorted before registration, so `forge
--help` lists commands in a stable order across runs and platforms.

**Testable in isolation.** The registry has its own test file. It
does not depend on which commands currently exist.

### What it does *not* do

- It does **not** execute commands. It registers them.
- It does **not** print. It logs.
- It does **not** render errors. It raises `RuntimeError` under
  `strict=True`, and the caller decides what to do with it.

### Dependencies

```
registry.py ──> pkgutil (iter_modules)
           ──> importlib (import_module)
           ──> typer (Typer, add_typer)
           ──> logging (warning)
```

### See also

- [ADR 0003](../adr/0003-use-a-file-based-command-registry.md) — why
  file-based over decorators, entry points, or a plugin system.
- [../guides/adding-a-command.md](../guides/adding-a-command.md) —
  how to add a command.

---

## `ui.py` — the voice

**File:** `src/forge/cli/ui.py`

**Responsibility:** render every user-facing message. One voice, one
stream per category.

### Public API

| Helper | Stream | Prefix | Colour | Category |
| :--- | :--- | :--- | :--- | :--- |
| `print_success` | stdout | ✓ | green | product |
| `print_warning` | stderr | ⚠ | yellow | commentary |
| `print_error` | stderr | ✗ | red | commentary |
| `print_info` | stdout | none | plain | product |
| `print_hint` | stderr | `Hint:` | dim | commentary |
| `print_cancelled` | stderr | none | dim | commentary |
| `typer_echo_safe` | stdout/stderr | none | plain | fallback |

### The stream contract

Product goes to **stdout**. Commentary goes to **stderr**. The
distinction is not cosmetic — it is the Unix contract. A script that
pipes `forge verify --json` to `jq` must receive valid JSON on
stdout, with no error messages mixed in.

See [../reference/streams.md](../reference/streams.md) for the full
contract and its rationale.

### The broken-pipe fallback

Every helper catches `BrokenPipeError` and falls back to
`typer_echo_safe`. This is what keeps `forge list | head` from
crashing when `head` closes the pipe. The fallback is *required*, not
optional: a CLI that crashes on a normal Unix idiom is a broken CLI.

### What it does *not* do

- It does **not** decide *which* helper to use for a given exception
  (that is `errors.py`'s job).
- It does **not** dispatch on exception type.
- It does **not** own the app.

### Dependencies

```
ui.py ──> rich.console.Console (one instance per stream)
      ──> typer (for the broken-pipe fallback)
```

### See also

- [ADR 0004](../adr/0004-centralize-cli-output-and-error-handling.md)
  — why one voice, why the stream split.
- [../reference/streams.md](../reference/streams.md) — the
  stdout/stderr contract.

---

## `errors.py` — the classifier

**File:** `src/forge/cli/errors.py`

**Responsibility:** classify an exception, render it with the right
helper, exit with the right code.

### Public API

| Symbol | Signature | Purpose |
| :--- | :--- | :--- |
| `handle_exception` | `(exc: BaseException, *, debug: bool) -> NoReturn` | The classifier. Dispatches on exception type. |

### The classification contract

| Exception category | Renderer | Exit code | `--debug` behavior |
| :--- | :--- | :--- | :--- |
| `ForgeError` | `print_error` + `print_hint` | `exc.exit_code` (1, 2, 3, 4, 5) | Render friendly (no change) |
| `click.ClickException` | `print_error` | `exc.exit_code` (usually 2) | Render friendly (no change) |
| `KeyboardInterrupt` / `typer.Abort` | `print_cancelled` | 1 | Render friendly (no change) |
| Anything else | `print_error` + `print_hint` | 5 | **Re-raise** |

The key insight: **`--debug` only affects the last category.** User
errors and cancellations are always rendered friendly. `--debug` is
for the *developer of Forge*, not the *user of Forge*.

### Why `SystemExit`, not `typer.Exit`

The handler is called from `main()`, *after* Typer's dispatch has
finished. `typer.Exit` is only special *inside* a Typer command.
Outside Typer, it propagates into `sys.exit(main())`, which prints a
traceback and exits 1. `SystemExit` is the exception `sys.exit`
expects; the handler raises it directly.

### What it does *not* do

- It does **not** print directly. It delegates to `ui.py`.
- It does **not** know about specific commands.
- It does **not** swallow exceptions. Every path exits or re-raises.

### Dependencies

```
errors.py ──> ui.py (print_error, print_hint, print_cancelled)
         ──> core.exceptions (ForgeError, ExitCode)
         ──> click (ClickException)
         ──> typer (Abort)
```

### See also

- [error-handling.md](error-handling.md) — the full contract.
- [ADR 0004](../adr/0004-centralize-cli-output-and-error-handling.md)
  — the reasoning.
- [../guides/adding-an-exception.md](../guides/adding-an-exception.md)
  — how to add a new `ForgeError` subclass.

---

## `__main__.py` — the fallback

**File:** `src/forge/cli/__main__.py`

**Responsibility:** enable `python -m forge.cli` as an alternate
entry point.

### Public API

```
python -m forge.cli <args>   # identical to `forge <args>`
```

### Why it exists

Windows environments where `forge` is not on `PATH`. CI containers.
Contributors running from source without an editable install. The
fallback is one file that calls `main()` — cheap insurance against
"it doesn't run on Windows" debugging.

### Dependencies

```
__main__.py ──> main.py (main)
```

---

## The empty `__init__.py`

**File:** `src/forge/cli/__init__.py`

**Responsibility:** none. It contains only a docstring.

### Why it is empty

Re-exporting `main` (or `create_app`) from `__init__.py` would bind
the *function* to the attribute `forge.cli.main` — shadowing the
*module* of the same name. After that shadowing,
`monkeypatch.setattr("forge.cli.main.get_version", ...)` would
resolve `forge.cli.main` via attribute lookup and find the
*function*, not the *module*, and fail.

The function is importable via `from forge.cli.main import main`,
which uses the import machinery and is unaffected by `__init__.py`
bindings. The empty `__init__.py` makes that distinction unambiguous.

### Design principle

A package's `__init__.py` should be the package's *front door*, not
its *house*. Every name it publishes is a promise to downstream
consumers. The safest `__init__.py` is the one that publishes
nothing — no shadowing, no circular imports, no broken
`monkeypatch.setattr`.

### See also

- [ADR 0003](../adr/0003-use-a-file-based-command-registry.md) — the
  registry pattern that motivated the shadowing fix.

---

## The command modules

Each command module in `cli/commands/` is a self-contained Typer
app. The contract is documented in `cli/commands/__init__.py`. The
short version:

```python
"""``forge ping`` — print 'pong' to confirm the CLI is working."""
from __future__ import annotations

import typer

app = typer.Typer(
    name="ping",
    help="Print 'pong' to confirm the CLI is working.",
)


@app.callback(invoke_without_command=True)
def ping() -> None:
    """Print 'pong' to confirm the CLI is working."""
    typer.echo("pong")
```

The six current commands:

| Module | Command | Status |
| :--- | :--- | :--- |
| `ping.py` | `forge ping` | Complete. |
| `list.py` | `forge list` | Stub. The blueprint discovery body lands in a later task. |
| `create.py` | `forge create` | Stub. The generation pipeline lands in a later task. |
| `validate.py` | `forge validate` | Stub. The blueprint validator lands in a later task. |
| `verify.py` | `forge verify` | Stub. The verification engine lands in a later task. |
| `doctor.py` | `forge doctor` | Stub. The diagnostics engine lands in a later task. |

The stubs are not placeholders. They are *contracts*: their argument
shapes, help text, and exit code contract are final. The bodies will
be filled in, but the *contract* the stubs declare will not change.

---

## The dependency graph

```
main.py ────────────────> registry.py
   │                          │
   │                          └──> commands/*.py ──> typer
   │
   ├──> errors.py ──> ui.py ──> rich
   │       │            │
   │       └──> click    └──> typer
   │
   └──> ui.py (version callback)
   │
   ├──> core/version.py ──> importlib.metadata
   │
   └──> core/exceptions.py
```

No cycles. `main.py` is the top of the graph; `core/exceptions.py`
is the bottom.

### Enforcing the graph

The dependency direction is enforced by *convention*, not by tooling
(there is no import-linter hook yet). The convention is: **`core/`
must not import from `cli/`**. Any future `generation/` or
`verification/` layer must follow the same rule.

If a contributor adds an import that violates the convention, it
will be caught in code review. If it becomes a recurring problem, an
import-linter hook will be added to CI.

---

## The tests

The test suite mirrors the module structure. One test file per
module, plus one for the entry point:

```
tests/
└── unit/
    └── cli/
        ├── test_main.py       # main.py
        ├── test_registry.py   # registry.py
        ├── test_ui.py         # ui.py
        └── test_errors.py     # errors.py
```

| Test file | Module | What it proves |
| :--- | :--- | :--- |
| `test_main.py` | `main.py` | App factory, entry point, version output, help output, `--debug` parsing. |
| `test_registry.py` | `registry.py` | Discovery, contract validation, graceful failure, strict mode, duplicate detection, `on_skip` callback. |
| `test_ui.py` | `ui.py` | Stream routing, prefixes, broken-pipe fallbacks, colour handling. |
| `test_errors.py` | `errors.py` | Four exception categories, `--debug` contract, no traceback leaks, exit-code mapping. |

The mirror is deliberate: **a failure in one test file points at
exactly one module.** A failure in `test_errors.py::TestNoTracebackLeaks`
points at the classifier's no-traceback invariant. A failure in
`test_registry.py::TestStrictMode` points at the registry's strict-mode
contract. There is no ambiguity about where to look.

---

## Further reading

- [overview.md](overview.md) — the whole codebase
- [error-handling.md](error-handling.md) — the classifier's contract
- [../reference/exit-codes.md](../reference/exit-codes.md) — the
  public exit-code contract
- [../reference/streams.md](../reference/streams.md) — the
  stdout/stderr contract
- [../guides/adding-a-command.md](../guides/adding-a-command.md) —
  how to add a command
- [../guides/adding-an-exception.md](../guides/adding-an-exception.md)
  — how to add an exception
- [../guides/running-tests.md](../guides/running-tests.md) — how to
  run the tests
- [../adr/](../adr/) — every architectural decision, with rationale
