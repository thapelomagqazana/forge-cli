# Architecture Overview

Forge CLI is a **modular monolith**: one process, one package, one
set of dependencies. Internally, it is divided into layers with
strict boundaries. This document is the map.

If you are new to the project, read this first. Then read
[cli-architecture.md](cli-architecture.md) for the CLI layer in
detail.

---

## What Forge CLI is

Forge CLI is a **project scaffolding and engineering foundation
management tool**. It turns reusable engineering knowledge into
working, verifiable software projects.

A user runs:

```bash
forge create python.fastapi.api --name payments-api
cd payments-api
forge verify
```

And receives a complete project — structure, dependencies, tests,
linting, CI, Docker — with a `.forge/manifest.json` that records
*which blueprint version* created it and *what configuration* was
chosen. The project is ready to build, test, and maintain, not
merely ready to edit.

That is the product. This document is about how the code that
delivers it is organized.

---

## The layers

Forge CLI is divided into layers. The layers are strictly ordered:
the CLI depends on the domain, the domain depends on the standard
library and well-justified third-party libraries. The reverse
direction is forbidden.

```
┌────────────────────────────────────────────────────────────────┐
│                       forge.cli (the CLI)                      │
│                                                                │
│  The user-facing interface. Typer commands, output helpers,    │
│  exception classifier, command registry.                       │
│                                                                │
│  Knows nothing about blueprints, templates, or filesystem.     │
│  Knows nothing about how projects are generated.               │
├────────────────────────────────────────────────────────────────┤
│                     forge.core (the domain)                    │
│                                                                │
│  Exceptions and exit codes, configuration, version             │
│  resolution.                                                   │
│                                                                │
│  Knows nothing about the CLI or Typer. Knows nothing about     │
│  how the CLI renders output or classifies exceptions.          │
├────────────────────────────────────────────────────────────────┤
│                 (future) domain layers                         │
│                                                                │
│  forge.blueprints, forge.generation, forge.verification,       │
│  forge.manifests, forge.diagnostics.                           │
│                                                                │
│  The engineering pipeline. Blueprint loading, template         │
│  rendering, filesystem safety, verification checks.            │
│                                                                │
│  Depends on forge.core. Must not depend on forge.cli.          │
└────────────────────────────────────────────────────────────────┘
```

### Why the direction matters

The dependency direction is **inward**: `cli → core → stdlib`. This
is the standard "clean architecture" rule, and it buys three things:

1. **The domain is testable without the CLI.** A future
   `forge.generation` module that renders a template can be tested
   by calling it directly. It does not need to start a Typer app,
   does not need to capture stdout, does not need to fake a terminal.
2. **The CLI is replaceable without the domain.** If Forge ever adds
   a web interface or a library API, the CLI can be swapped for a
   different front-end without changing a single line of the
   domain. The domain does not know the CLI exists.
3. **The core is stable.** `forge.core.exceptions` and
   `forge.core.version` are depended on by everything. Changing
   them is a *breaking change* by definition. The layers above them
   are free to change because nothing depends on them.

---

## The layers in detail

### `forge.cli` — the CLI

**Responsibility:** the user-facing interface. Parse arguments,
discover commands, render output, classify exceptions, exit.

**What it owns:**

| Concern | File |
| :--- | :--- |
| The process entry point (`main()`) and the app factory (`create_app()`) | `cli/main.py` |
| Command discovery and registration | `cli/registry.py` |
| The CLI's voice (six output helpers) | `cli/ui.py` |
| The exception classifier | `cli/errors.py` |
| The six commands | `cli/commands/*.py` |
| The `python -m forge.cli` fallback | `cli/__main__.py` |

**What it does not own:** blueprint loading, template rendering,
filesystem operations, verification checks, manifest reading,
diagnostics. Those belong to the future domain layers.

**Design principles:**

- **Thin.** The CLI does not implement business logic. It parses,
  dispatches, renders, exits.
- **One voice.** Every user-facing message goes through
  `cli/ui.py`. The format cannot drift.
- **Classify before rendering.** The exception's category determines
  its rendering, exit code, and `--debug` behavior. See
  [error-handling.md](error-handling.md).

### `forge.core` — the domain

**Responsibility:** the shared foundation that everything depends on.
Exceptions, exit codes, configuration, version.

**What it owns:**

| Concern | File |
| :--- | :--- |
| The `ExitCode` enum and the `ForgeError` hierarchy | `core/exceptions.py` |
| Configuration (via `pydantic-settings`) | `core/config.py` |
| Version resolution (four-step fallback chain) | `core/version.py` |

**What it does not own:** anything that renders, parses arguments,
or talks to the user. The core is *headless* — it produces values
(exceptions, exit codes, configuration objects, version strings),
and the CLI decides what to do with them.

**Design principles:**

- **Stable.** Every change to the core is a breaking change for
  everything above it. Changes require a major version bump.
- **Small.** The core has three files. It grows only when a concern
  is *genuinely shared* between the CLI and the future domain
  layers.
- **Typed.** Every function has type hints. `mypy --strict` runs on
  the core with no exceptions.

### Future domain layers

The following layers will be added as the project grows. Each one
follows the same rule: depend on `forge.core`, not on `forge.cli`.

- **`forge.blueprints`** — blueprint loading, validation, the
  `blueprint.yaml` schema and its Pydantic models.
- **`forge.generation`** — template rendering, staging, commit,
  rollback, manifest generation.
- **`forge.verification`** — check registry, check handlers, result
  aggregation.
- **`forge.manifests`** — project manifest reading, validation,
  migration.
- **`forge.diagnostics`** — environment checks, project health.

Each layer will have its own directory under `src/forge/`, its own
test directory under `tests/unit/`, and its own ADR (if the design
warrants one).

---

## The directory tree

```
src/forge/
├── __init__.py              # Library-facing API (__version__, get_version)
├── _version.py              # Generated by hatch-vcs (gitignored)
├── py.typed                 # PEP 561 marker
├── cli/
│   ├── __init__.py          # Intentionally empty (no re-exports)
│   ├── __main__.py          # `python -m forge.cli` fallback
│   ├── main.py              # Entry point (main, create_app)
│   ├── registry.py          # Command discovery
│   ├── ui.py                # Output helpers (the voice)
│   ├── errors.py            # Exception classifier
│   └── commands/
│       ├── __init__.py      # The command module contract
│       ├── list.py          # `forge list`
│       ├── create.py        # `forge create`
│       ├── validate.py      # `forge validate`
│       ├── verify.py        # `forge verify`
│       ├── doctor.py        # `forge doctor`
│       └── ping.py          # `forge ping`
└── core/
    ├── __init__.py
    ├── config.py            # Settings (pydantic-settings)
    ├── exceptions.py        # ExitCode, ForgeError, subclasses
    └── version.py           # get_version (four-step fallback)
```

---

## The flow of a command

When a user runs `forge ping`:

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
   │       └── `ping()` prints `pong` via `typer.echo`
   └── raises `SystemExit(ExitCode.SUCCESS)`

3. Console script calls `sys.exit(main())`
   └── exits 0
```

If a command raises, the exception flows back through `main()`'s
`try/except`, into `errors.handle_exception()`, which classifies it,
renders it, and raises `SystemExit` with the appropriate code. See
[error-handling.md](error-handling.md).

The key insight: **`main()` is a dispatcher, not a renderer**. It
creates the app, delegates registration, delegates error handling,
and exits. The *content* of each operation belongs to a specialist
module. This is what makes the CLI testable — each module can be
tested in isolation, without spinning up the whole app.

---

## The tests

Tests mirror the module structure exactly. This is deliberate: a
failure in one test file points at exactly one module.

```
tests/
└── unit/
    ├── cli/
    │   ├── test_main.py       # Entry point + app factory
    │   ├── test_registry.py   # Command discovery
    │   ├── test_ui.py         # Output helpers
    │   └── test_errors.py     # Exception classifier
    └── core/
        ├── test_version.py    # Version resolver
        └── test_config.py     # Settings
```

| Test file | Module under test | What it proves |
| :--- | :--- | :--- |
| `test_main.py` | `cli/main.py` | App factory, entry point, version output, help output, `--debug` parsing. |
| `test_registry.py` | `cli/registry.py` | Discovery, contract validation, graceful failure, strict mode, duplicate detection, `on_skip` callback. |
| `test_ui.py` | `cli/ui.py` | Stream routing, prefixes, broken-pipe fallbacks, colour handling. |
| `test_errors.py` | `cli/errors.py` | Four exception categories, `--debug` contract, no traceback leaks, exit-code mapping. |
| `test_version.py` | `core/version.py` | Four-step fallback chain, precedence, caching, PEP 440 validity. |
| `test_config.py` | `core/config.py` | Defaults, environment override. |

The test suite is designed so that a failure names the decision that
broke. A failure in `test_errors.py::TestNoTracebackLeaks` points at
the handler's no-traceback invariant, not at the app factory. A
failure in `test_registry.py::TestStrictMode` points at the
registry's strict-mode contract, not at the command modules. This is
what "one test file per module" buys: *zero ambiguity about where to
look*.

---

## The principles

Forge CLI is built on eight principles. Each is grounded in an ADR
(Architecture Decision Record) in `docs/adr/`. The ADR records *why*
the principle was chosen, *what alternatives* were considered, and
*what consequences* follow.

| # | Principle | ADR |
| :--- | :--- | :--- |
| 1 | **CLI-first.** The command line is the primary interface. Every workflow is predictable, discoverable, and efficient. | [0001](../adr/0001-use-typer-for-the-cli-layer.md) |
| 2 | **Version from metadata.** `--version` reads the installed package version. No hard-coded strings, no drift. | [0002](../adr/0002-use-importlib-metadata-for-version-resolution.md) |
| 3 | **File-based commands.** Adding a command means adding a file. The entry point never changes. | [0003](../adr/0003-use-a-file-based-command-registry.md) |
| 4 | **One voice.** Every message goes through `cli/ui.py`. The format cannot drift. | [0004](../adr/0004-centralize-cli-output-and-error-handling.md) |
| 5 | **Classify before rendering.** The exception's category determines its rendering, exit code, and `--debug` behavior. | [0004](../adr/0004-centralize-cli-output-and-error-handling.md) |
| 6 | **Product to stdout, commentary to stderr.** The Unix contract. | [0004](../adr/0004-centralize-cli-output-and-error-handling.md) |
| 7 | **No tracebacks for users.** A stack trace is a developer message, not a user message. | [0004](../adr/0004-centralize-cli-output-and-error-handling.md) |
| 8 | **Test the contract, not the implementation.** A test that breaks when the implementation changes but the contract doesn't is a test that tests too much. | [0004](../adr/0004-centralize-cli-output-and-error-handling.md) |

The ADRs are not ceremony. They are the *reason* the code looks the
way it does. A future contributor who asks "why not just use
`print()`?" finds the answer in ADR 0004. A future contributor who
asks "why not just import every command in `main.py`?" finds the
answer in ADR 0003. The ADRs prevent re-litigation.

---

## Where to find things

| Question | Answer |
| :--- | :--- |
| **"I want to add a new command."** | [`guides/adding-a-command.md`](../guides/adding-a-command.md). Short version: add a file to `cli/commands/`. |
| **"I want to change how errors are rendered."** | `cli/errors.py`. Read [`architecture/error-handling.md`](error-handling.md) first. |
| **"I want to add a new exception type."** | `core/exceptions.py`. Read [`guides/adding-an-exception.md`](../guides/adding-an-exception.md). |
| **"I want to understand the exit-code contract."** | [`reference/exit-codes.md`](../reference/exit-codes.md). |
| **"I want to understand which stream a message goes to."** | [`reference/streams.md`](../reference/streams.md). |
| **"I want to understand why a decision was made."** | `docs/adr/`. Every significant decision has an ADR. |
| **"I want to run the tests."** | [`guides/running-tests.md`](../guides/running-tests.md). Short version: `make test`. |
| **"I want to set up my environment."** | [`guides/development-setup.md`](../guides/development-setup.md). Short version: `make install`. |
| **"I want to understand the CLI layer in detail."** | [`architecture/cli-architecture.md`](cli-architecture.md). |
| **"I want to see the current status of the project."** | `docs/status.md`. |

---

## The status of the project

Forge CLI is at **alpha (v0.1.0a0)**. The command surface — `list`,
`create`, `validate`, `verify`, `doctor`, `ping` — is complete and
every command is registered by the file-based registry. The command
*bodies* are stubs that will be filled in by later tasks.

What is done:

- ✅ The CLI entry point, app factory, and version resolution.
- ✅ The file-based command registry and the six command modules.
- ✅ The output helpers (the CLI's voice).
- ✅ The exception classifier.
- ✅ The exception hierarchy and exit-code contract.
- ✅ The test suite (137 tests, 91.94% coverage).
- ✅ The ADR system and four ADRs.

What is next:

- 🚧 The generation pipeline (`forge create` body).
- 🚧 The verification engine (`forge verify` body).
- 🚧 The diagnostics engine (`forge doctor` body).
- 🚧 The three official blueprints (`python.fastapi.api`,
  `typescript.react.app`, `python.pytest.automation`).

See `CHANGELOG.md` for the full release history and
`docs/status.md` for the detailed status.

---

## Further reading

- [cli-architecture.md](cli-architecture.md) — the CLI layer in
  detail: modules, contracts, dependency graph
- [error-handling.md](error-handling.md) — the classifier's contract
- [../reference/exit-codes.md](../reference/exit-codes.md) — the
  public exit-code contract
- [../reference/streams.md](../reference/streams.md) — the
  stdout/stderr contract
- [../guides/adding-a-command.md](../guides/adding-a-command.md) —
  how to add a command
- [../guides/adding-an-exception.md](../guides/adding-an-exception.md)
  — how to add an exception
- [../adr/](../adr/) — every architectural decision, with rationale
- [../CONTRIBUTING.md](../../CONTRIBUTING.md) — the contribution
  workflow
