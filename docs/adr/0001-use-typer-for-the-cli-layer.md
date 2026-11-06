# 0001. Use Typer for the CLI layer

- **Status:** Accepted
- **Date:** 2026-11-06
- **Deciders:** Thapelo Magqazana
- **Consulted:** —
- **Informed:** Forge CLI contributors

## Context

Forge CLI is a developer tool. It must feel fast, predictable, and
discoverable to engineers who spend most of their day in a terminal. Those
engineers expect:

- **Subcommands with clear help text** (`forge create --help` explains every
  option without requiring a man page).
- **Shell completion** (`forge cr<TAB>` completes to `create`) for bash, zsh,
  fish, and PowerShell.
- **Machine-readable output** (`--json`) alongside human-readable output, so
  the CLI can be used in CI scripts as well as interactively.
- **Typed arguments** so that invalid input fails before any filesystem
  mutation occurs.
- **Cross-platform behaviour** — the same commands work on Linux, macOS, and
  Windows, with consistent exit codes and error messages.
- **Testability** — contributors must be able to invoke the CLI in-process
  from pytest without spawning subprocesses, so the test suite stays fast.

The project also has constraints:

- **Python 3.12+ only.** No legacy Python support means modern type hints
  (`X | Y`, `match`, `typing.override`) are available everywhere.
- **Strict typing.** `mypy --strict` runs on every commit. The CLI framework
  must support complete type annotations without `# type: ignore` escape
  hatches.
- **Minimal dependency surface.** Every runtime dependency is a liability.
  The CLI framework must be well-maintained, widely used, and small in
  transitive dependency count.
- **Single-binary distribution goal (long-term).** Forge should eventually
  be installable as a standalone binary (e.g., via PyInstaller or Nuitka).
  The CLI framework must not preclude this.

At the time of this decision, no CLI framework had been chosen. The
`src/forge/cli/main.py` file was a placeholder; `pyproject.toml` listed
`typer>=0.12.0,<1.0` as a provisional dependency but with no recorded
rationale. This ADR records the reasoning so that the choice is not
silently revisited.

## Decision

We will use **Typer 0.12+** as the CLI framework for Forge CLI.

Specifically:

- **Typer is the single framework** for defining commands, arguments,
  options, and help text. No mixing with `argparse`, `click`, or `docopt`.
- **Type hints are the source of truth.** Command signatures use Python
  type annotations (`Annotated[str, typer.Option(...)]`) rather than
  decorators or configuration dictionaries. This keeps the CLI definition
  identical to the function signature, which `mypy --strict` validates.
- **Click remains an indirect dependency** (Typer is built on Click) but is
  not used directly. If a Click feature is ever needed, we will expose it
  through a Typer wrapper rather than importing Click in command modules.
- **Shell completion is enabled** via `add_completion=True` on the root app,
  which emits the correct shell-specific completion instructions.
- **Rich is the rendering layer** for terminal output (tables, panels,
  progress bars, colours). Typer integrates with Rich natively, so the two
  are chosen as a pair.
- **The root app is a factory** (`create_app()`), not a module-level
  constant. This makes the app re-creatable for tests and for future
  multi-app scenarios (e.g., a `forge-admin` CLI that reuses the same
  commands).

## Consequences

### Positive

- **Type-safe command definitions.** Every argument and option is typed.
  `mypy --strict` verifies the types of every command signature, so a
  misspelled option or a wrong default is caught at type-check time, not at
  runtime. This is the single biggest reason for choosing Typer over Click
  directly.

- **Automatic help text.** Typer generates help from the function
  docstring and the `help=` parameters on options. A well-documented
  command function produces well-documented help text with no duplication.

- **Automatic shell completion.** `add_completion=True` emits the
  completion instructions for bash, zsh, fish, and PowerShell. Typer
  derives the completions from the type annotations, so completing a
  `Literal["a", "b"]` argument offers exactly `a` and `b`.

- **Testability without subprocesses.** `Typer.CliRunner` invokes the app
  in-process, captures `stdout`/`stderr`, and returns the exit code. Unit
  tests run in milliseconds rather than hundreds of milliseconds. The full
  CLI test suite stays under five seconds.

- **Rich integration.** Typer detects Rich and uses it automatically for
  help rendering, error rendering, and progress bars. There is no need to
  bridge the two frameworks manually.

- **Small dependency footprint.** Typer depends on Click and
  `typing-extensions`. Rich is a direct dependency but is chosen
  independently for terminal rendering. The total transitive dependency
  count is well under what `argparse`-adjacent frameworks (e.g.,
  `docopt`, `cliff`) would require, and it is smaller than a full TUI
  framework like `textual`.

- **Cross-platform consistency.** Typer delegates terminal handling to
  Click, which has been solving cross-platform CLI problems since 2014.
  Colours, line endings, and argument parsing behave identically on
  Linux, macOS, and Windows.

### Negative

- **Click is a transitive dependency.** Typer is built on Click, so Click
  is installed whether we use it directly or not. This is a small cost —
  Click is stable, well-maintained, and widely deployed — but it is a
  dependency we do not control.

- **Typer's release cadence is slower than Click's.** New Click features
  appear in Typer only after Typer wraps them. For most features this is
  irrelevant, but for cutting-edge terminal features there may be a lag.
  In practice, the features Forge needs (subcommands, options, completion,
  Rich integration) have been stable in Typer since 0.7.

- **The `Annotated[...]` style is verbose.** A command signature like
  `name: Annotated[str, typer.Option("--name", "-n", help="...")]` is
  longer than the equivalent Click decorator. This is a readability cost
  that is paid once per command, in exchange for type safety everywhere
  else.

- **Documentation is thinner than Click's.** Typer's documentation is good
  but not as exhaustive as Click's. Contributors occasionally need to
  read Click's docs to understand Typer behaviour. This is documented in
  `CONTRIBUTING.md` so contributors know where to look.

- **Typer 0.x is still pre-1.0.** The `>=0.12.0,<1.0` range means a
  future 1.0 release may introduce breaking changes. We mitigate this by
  pinning a minor range (`<1.0`) and by treating the upgrade to 1.0 as a
  planned event with a migration PR, not an automatic `uv lock --upgrade`.

### Follow-up actions

- [x] Pin `typer>=0.12.0,<1.0` in `pyproject.toml`.
- [x] Configure `add_completion=True` on the root app.
- [x] Use `Annotated` style for all command signatures (see
      `CONTRIBUTING.md` for examples).
- [x] Document the Typer testing pattern (`Typer.CliRunner`) in
      `CONTRIBUTING.md`.
- [ ] Revisit this ADR when Typer 1.0 is released. At that point, create
      ADR NNNN (superseding this one) that records whether we upgrade and
      what breaking changes need migration.
- [ ] Evaluate Typer's performance on the largest blueprint generation
      (target: startup under 300ms). If startup regresses, profile and
      either optimize the command tree or consider Click directly as a
      superseding decision.
- [ ] Audit the transitive dependency count annually. If Click's own
      dependency count grows significantly, revisit the choice.

## Alternatives Considered

### Alternative 1: Click directly

Click is the framework Typer is built on. It is mature (since 2014),
widely used (Flask, Black, pip, and many others), and has excellent
documentation. It supports subcommands, options, shell completion, and
Rich integration.

**Why rejected:** Click's command definitions are decorator-based, which
means the CLI definition is *separate from* the function signature. Type
hints on the function arguments are not used by Click; the option types
are declared in the decorator. This means `mypy --strict` cannot verify
the types of options, and contributors must remember to keep the decorator
and the signature in sync manually. For a codebase that runs `mypy
--strict` on every commit, this is a significant ergonomic loss. Typer's
`Annotated` style keeps the CLI definition and the function signature
unified, which is the property we optimized for.

Click remains a transitive dependency (Typer is built on it). If Typer
ever becomes unmaintained, migrating to Click directly is a viable
path — the migration is mechanical, not architectural.

### Alternative 2: argparse (standard library)

`argparse` is in the standard library, so it adds zero dependencies. It
supports subcommands, arguments, options, and help text.

**Why rejected:** `argparse` requires significant boilerplate for every
command, has no shell completion, does not integrate with Rich, and its
help text formatting is limited. More importantly, `argparse` is not
type-driven — every argument type is a string parsed at runtime, so
`mypy --strict` cannot verify the command surface. For a project whose
primary quality gate is `mypy --strict`, `argparse` would require either
(a) extensive manual type checking in every command, or (b) accepting
that the CLI layer is untyped. Neither is acceptable.

`argparse` also does not support nested subcommands cleanly (e.g.,
`forge blueprint validate`), which Forge needs for blueprint authoring.

### Alternative 3: docopt / docopt-ng

`docopt` derives the CLI from the help text itself: you write the usage
message, and the parser is generated from it. This is elegant in theory —
one source of truth for help and parsing.

**Why rejected:** `docopt` is unmaintained (the original project has been
dormant since 2016; `docopt-ng` is a community fork but has a small
maintainer base). The type safety story is worse than `argparse`: the
parser is generated from a string, so there is no way for `mypy` to
verify the command surface at all. Shell completion is not supported.
Rich integration is not supported. For a production CLI that must run
for years, betting on an unmaintained library is a risk we are not
willing to take.

### Alternative 4: A hand-rolled parser

A hand-rolled parser gives complete control: no dependencies, complete
type safety (if written carefully), and total flexibility.

**Why rejected:** A hand-rolled parser is a multi-month project to match
what Typer provides out of the box (subcommands, options, completion,
help rendering, Rich integration, error handling, cross-platform
consistency). The maintenance cost would exceed the cost of any of the
other alternatives. The only situation in which a hand-rolled parser is
justified is when no existing parser meets the requirements — and Typer
meets every requirement Forge has.

## References

- Typer documentation: https://typer.tiangolo.com/
- Typer source: https://github.com/fastapi/typer
- Click documentation: https://click.palletsprojects.com/
- Rich documentation: https://rich.readthedocs.io/
- Michael Nygard, "Documenting Architecture Decisions":
  https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- ADR 0001 (this document) is referenced by the `pyproject.toml`
  dependency block and by `src/forge/cli/main.py`.
