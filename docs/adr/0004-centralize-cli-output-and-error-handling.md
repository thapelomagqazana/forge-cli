# 0004. Centralize CLI output and error handling

- **Status:** Accepted
- **Date:** 2026-11-07
- **Deciders:** Thapelo Magqazana
- **Consulted:** —
- **Informed:** Forge CLI contributors

## Context

Before this decision, every command module printed its own output:

- `list.py` used `typer.echo`.
- `create.py` used `typer.echo`.
- `main.py` used a module-level `rich.console.Console` and an inline
  `_handle_exception` function.
- The command stubs used `typer.echo` with no consistent prefix.

The result was an inconsistent voice. Some messages had a checkmark,
some had a colon, some were plain text. There was no single place to
change the format, and no single place to test it.

Error handling was worse. The inline handler in `main.py` mixed three
concerns:

1. Rendering `ForgeError` subclasses.
2. Rendering unexpected exceptions.
3. Deciding the exit code.

Testing it required invoking the app through `Typer.CliRunner`, which
intercepts exceptions before the handler runs. The handler's most
important property — "never leak a traceback" — could not be tested
directly. It could only be inferred from the app's output.

The constraints on the solution are:

1. **Consistent voice.** Every message goes through the same helpers.
2. **Correct streams.** Product to stdout, commentary to stderr.
3. **No tracebacks for users.** `ForgeError` and unexpected
   exceptions never print a traceback by default.
4. **Tracebacks for developers.** `--debug` shows the traceback.
5. **Testable in isolation.** The handler can be exercised without
   going through the app.
6. **Stable exit codes.** Every failure mode maps to a documented
   exit code that scripts can branch on.

The question, then, is not *whether* to centralize output and error
handling. The question is *how*.

## Decision

We will split the CLI's "voice" into two modules:

### `forge.cli.ui` — the voice

Five output helpers, each with a distinct stream, prefix, and colour:

| Helper | Stream | Colour | Prefix | Category |
| :--- | :--- | :--- | :--- | :--- |
| `print_success` | stdout | green | ✓ | Product (success) |
| `print_warning` | stderr | yellow | ⚠ | Commentary (warning) |
| `print_error` | stderr | red | ✗ | Commentary (error) |
| `print_info` | stdout | plain | none | Product (info) |
| `print_hint` | stderr | dim | `Hint: ` | Commentary (hint) |
| `print_cancelled` | stderr | dim | none | Commentary (cancellation) |

Every command, every check handler, and every future feature uses
these helpers. No command calls `typer.echo` or `print` directly.

The stream split is the Unix contract: `stdout` is the *product* of
the command (the result the user asked for), `stderr` is the
*commentary* (errors, warnings, progress, diagnostics). A script that
pipes `forge list --json` to `jq` must receive valid JSON on stdout,
with no error messages mixed in.

### `forge.cli.errors` — the handler

A single function, `handle_exception(exc, *, debug)`, that renders
every uncaught exception. It dispatches on the exception type:

- `ForgeError` and subclasses → `print_error` + optional
  `print_hint` + exit with the subclass's `exit_code`.
- `KeyboardInterrupt` / `typer.Abort` → `print_cancelled` + exit 1.
- Anything else → generic "unexpected internal error" + hint to run
  with `--debug` + exit 5.
- Under `debug=True` → re-raise the exception (the developer sees
  the traceback).

The handler is tested in isolation
(`tests/unit/cli/test_errors.py`), not through `CliRunner`.

### `main.py`

The entry point delegates to `handle_exception` and to `ui`. It
contains no error rendering and no module-level console. It is now
purely the app factory and the process entry point.

### `ForgeError`

The base class gains a `hint` attribute (first-class, not just a
`getattr` fallback in the handler). The four subclasses
(`UserInputError`, `BlueprintError`, `FilesystemConflictError`,
`VerificationError`) each declare a distinct `exit_code`, so scripts
can branch on the code without parsing the message.

## Consequences

### Positive

- **Consistent voice.** Every message goes through the same helpers.
  The format cannot drift.

- **Correct streams.** `print_error` and `print_warning` write to
  stderr, `print_success` and `print_info` to stdout. The Unix
  contract is honoured. Machine-readable output (e.g.,
  `forge verify --json`) cannot be polluted by error or warning
  messages.

- **No tracebacks for users.** The handler never prints a traceback
  for `ForgeError` or unexpected exceptions. Every path through the
  handler is tested (`TestNoTracebackLeaks`) to prove it.

- **Tracebacks for developers.** `--debug` re-raises the exception,
  so the developer sees the full traceback. The contract is strict:
  the flag is a *promise* to show the traceback, not a request.

- **Testable in isolation.** The handler can be tested directly,
  without `CliRunner`. Its most important property (no traceback)
  is tested directly, not inferred.

- **Exit codes are stable.** Each `ForgeError` subclass has a
  distinct exit code. Scripts can branch on the code without parsing
  the message. Adding a subclass is non-breaking; removing one is
  breaking.

- **Cancellation is its own category.** A cancellation (Ctrl+C,
  `typer.Abort`) is neither info nor warning nor error. It gets its
  own helper (`print_cancelled`) and its own stream (stderr). The
  user who pressed Ctrl+C sees a single dimmed "Cancelled." line,
  not an "Error!" prefix.

### Negative

- **Two more modules.** `ui.py` and `errors.py` are new. This is a
  small cost for a large correctness win.

- **Two streams to think about.** New commands must remember that
  success/info goes to stdout, warnings/errors/cancellations to
  stderr. This is documented in `ui.py` and enforced by the helpers
  (each helper writes to the correct stream), but it is still a
  convention that must be learned.

- **Colour is a courtesy, not a requirement.** The helpers emit
  colour codes, but they must be readable in plain text. The
  prefixes (✓ ⚠ ✗ `Hint: `) are the signal; colour is additive.
  This is documented in `ui.py` but is a discipline that must be
  maintained.

- **The handler is a hotspot.** Every uncaught exception goes
  through it. A bug in the handler affects every error path. The
  handler is small (four branches) and heavily tested, but it is
  still the single most important file in the CLI.

- **The voice is a hotspot too.** Every user-facing message goes
  through `ui.py`. A bug in a helper (e.g., writing to the wrong
  stream) affects every command. The helpers are covered by a
  dedicated test file (`tests/unit/cli/test_ui.py`) that asserts on
  stream, prefix, and broken-pipe fallback.

- **Broken-pipe fallbacks are required.** Every helper must catch
  `BrokenPipeError` and fall back to `typer.echo`. This is what
  keeps `forge list | head` from crashing when `head` closes the
  pipe. The fallback is defensive and adds a branch per helper, but
  it is a *requirement*, not a nicety.

### Follow-up actions

- [x] Create `forge.cli.ui` with five output helpers and one hint
      helper.
- [x] Create `forge.cli.errors` with a single `handle_exception`
      function.
- [x] Update `main.py` to delegate to both modules.
- [x] Add `hint` as a first-class attribute on `ForgeError`.
- [x] Add the four `ForgeError` subclasses with distinct exit codes.
- [x] Add `print_cancelled` after the cancellation branch was found
      to write to stdout (the test suite caught it).
- [x] Write `tests/unit/cli/test_ui.py` (voice: stream, prefix,
      broken-pipe fallback).
- [x] Write `tests/unit/cli/test_errors.py` (handler: every clause,
      no traceback leaks).
- [x] Trim `tests/unit/cli/test_main.py` to entry-point tests only.
- [ ] Migrate the command module stubs to use `ui.print_*` instead
      of `typer.echo`. (Deferred to the generation pipeline task,
      which will rewrite the stubs' bodies anyway.)
- [ ] Add a `--no-color` global flag that sets `NO_COLOR` for the
      process. (Deferred to the configuration task.)
- [ ] Add structured logging (via the `logging` module, routed to
      stderr) for `--debug` mode. (Deferred to the diagnostics task.)
- [ ] Add a `print_progress` helper for long-running operations
      (e.g., "Rendering 5/20 files"). (Deferred to the generation
      pipeline task.)

## Alternatives Considered

### Alternative 1: Keep the inline handler in `main.py`

**Why rejected:** The inline handler cannot be tested in isolation,
because `CliRunner` intercepts exceptions before they reach it. Its
most important property — "no traceback for users" — could only be
tested indirectly, by invoking the app and checking the output. This
is fragile: a future refactor could break the handler in a way that
the app-level test does not catch. The isolated handler is a
four-branch module with 41 direct tests; every branch is covered.

### Alternative 2: Use `typer.echo` everywhere, no `ui.py`

**Why rejected:** `typer.echo` writes to stdout by default. Warnings
and errors would need `err=True` on every call. There is no prefix
convention, so each command would invent its own. There is no colour
handling, so each command would invent its own. The result is
exactly the inconsistency this task exists to fix.

### Alternative 3: A full logging framework (stdlib `logging`)

**Why rejected:** The CLI's output is not *logging* — it is
*user-facing communication*. Logging is for diagnostics, debugging,
and operational telemetry. It has levels (DEBUG, INFO, WARNING,
ERROR), handlers, formatters, and filters. The CLI needs five helpers
and one hint helper. Adding a full logging framework would be
over-engineering, and it would tempt future contributors to add
`log.info()` calls that the user sees, which is exactly the
inconsistency we are trying to prevent.

The `logging` module *will* be used for `--debug` diagnostics
(follow-up action). It is the right tool for that, and the wrong
tool for user-facing output.

### Alternative 4: A third-party CLI output library (`rich` only)

**Why rejected:** Rich is already a dependency, and `ui.py` uses
Rich's `Console` internally. But Rich's API is too low-level for a
consistent voice: `console.print("[green]✓[/green] done")` is not
the same as `print_success("done")`. The helper layer is thin, but it
is the layer that enforces the convention. Without it, every command
would call `console.print` with its own markup.

### Alternative 5: Let each command define its own error classes

**Why rejected:** A command-specific error class is a command-specific
exit code is a command-specific contract. Scripts cannot branch on
exit codes if every command invents its own. The `ForgeError`
hierarchy is the *shared* contract: every error goes through the same
handler, every handler maps to the same exit-code table. Adding a new
error class is fine, but it must live in `forge.core.exceptions` and
declare its exit code, not in the command module.

### Alternative 6: Route all output through a single helper with a level parameter

```python
ui.print_message("boom", level="error")
```

**Why rejected:** A single helper with a level parameter is a
*dispatch function*, not a *voice*. The caller has to remember the
level string, which is exactly the kind of freedom that produces
inconsistency. Worse, the level parameter is a *string*, so a typo
(`level="eror"`) silently falls through to a default. Five separate
helpers, each with its own name, are self-documenting: `print_error`
is a verb, `print_message(level="error")` is a configuration.

The same argument applies to `print(..., stream=sys.stderr)`. The
stream is not a parameter — it is determined by the *category* of
the message, and the category is the *name* of the helper.

## References

- Rich `Console` documentation:
  https://rich.readthedocs.io/en/stable/console.html
- Typer `Exit` documentation:
  https://typer.tiangolo.com/tutorial/exceptions/
- Unix standard streams (stdin, stdout, stderr):
  https://en.wikipedia.org/wiki/Standard_streams
- PEP 561 (typed packages, relevant to `py.typed`):
  https://peps.python.org/pep-0561/
- ADR 0001 (Use Typer for the CLI layer), which established the CLI
  that this handler plugs into.
- ADR 0002 (Use importlib.metadata for version resolution), which
  established the `--version` output that goes through `print_info`.
- ADR 0003 (Use a file-based command registry), which established the
  command structure that these helpers are used by.
- Implementation: `src/forge/cli/ui.py`, `src/forge/cli/errors.py`,
  `src/forge/core/exceptions.py`
- Tests: `tests/unit/cli/test_ui.py`, `tests/unit/cli/test_errors.py`
