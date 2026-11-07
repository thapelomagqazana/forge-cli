# Error Handling

This document describes the exception classifier (`forge.cli.errors`)
and the contract it enforces. It is the single most important file
for anyone changing error paths.

The document has one job: make the contract *executable in prose*.
Every claim is backed by a test in `tests/unit/cli/test_errors.py`.
The prose explains *why*; the tests prove *what*. Read them together.

If you are adding a new exception type, read
[../guides/adding-an-exception.md](../guides/adding-an-exception.md)
first.

---

## The contract

`handle_exception(exc, *, debug)` classifies an exception into one of
four categories and dispatches to the matching renderer:

| Category | Examples | Renderer | Exit code | `--debug` behavior |
| :--- | :--- | :--- | :--- | :--- |
| **`ForgeError`** | `UserInputError`, `BlueprintError`, `FilesystemConflictError`, `VerificationError` | `print_error` + `print_hint` | `exc.exit_code` (1, 2, 3, 4, 5) | Render friendly (no traceback) |
| **`click.ClickException`** | `UsageError`, `BadParameter`, `Abort` | `print_error` | `exc.exit_code` (usually 2) | Render friendly (no traceback) |
| **Cancellation** | `KeyboardInterrupt`, `typer.Abort` | `print_cancelled` | 1 | Render friendly (no traceback) |
| **Unexpected exception** | `RuntimeError`, `ValueError`, `OSError`, etc. | `print_error` + `print_hint` | 5 | **Re-raise** (developer sees traceback) |

The key insight: **`--debug` only affects the last category.** User
errors (`ForgeError`, `ClickException`) and cancellations are always
rendered friendly. `--debug` is for the *developer of Forge*, not the
*user of Forge*. It exists so the developer can see the traceback of
a *bug*, not so the user can see the traceback of their own typo.

---

## Why these categories

The categories are not arbitrary. Each corresponds to a different
*kind* of failure, with a different *audience* and a different
*correct response*.

### `ForgeError` — a failure in Forge's own code that the user can fix

The user did something Forge recognizes as invalid: a missing flag,
a blueprint that doesn't validate, a filesystem collision. The
correct response is a friendly message with a hint: *what went
wrong* and *what to do next*. Exit code from the subclass, so scripts
can distinguish the failure modes.

The four subclasses each have a distinct exit code (see
[../reference/exit-codes.md](../reference/exit-codes.md)). A script
can branch on `$?` without parsing the message:

```bash
forge create python.fastapi.api --name "My Project"
case $? in
    0) echo "success" ;;
    1) echo "user error: check your input" ;;
    2) echo "blueprint error: check the blueprint" ;;
    3) echo "filesystem conflict: choose a different output" ;;
    5) echo "internal error: report a bug" ;;
esac
```

### `click.ClickException` — a failure in Click's parser, re-raised by Typer

The user typed a command that doesn't exist, or passed an argument
that doesn't match the signature. These are *also* user errors, but
they come from a different layer (Click, not Forge). The correct
response is the same: a friendly message. Exit code from Click (2 for
usage errors).

Why a separate category? Because Click's exception messages are
already user-facing (e.g., `"No such command 'nonsense-command'."`),
and Click's exit codes are already conventional (2 for usage errors).
The classifier does not need to wrap them — it just needs to render
them with the right helper and exit with the right code.

### Cancellation — the user pressed Ctrl+C, or declined a prompt

This is not a failure at all: the user told the command to stop. The
correct response is a single dimmed "Cancelled." line on stderr, exit
1 (non-zero, so scripts know the command didn't complete).

A cancellation is *not* an error. It is not a warning. It is not
info. It is its own category, with its own helper
(`ui.print_cancelled`). The message goes to stderr because it is
commentary, not product.

### Unexpected exception — a bug in Forge

The user did nothing wrong; Forge's code has a defect. The correct
response is a generic message ("An unexpected internal error
occurred"), a link to the issue tracker, and exit 5. The message
*never* includes the exception's class, message, or traceback — those
are internal details that could leak the user's paths or hint at a
vulnerability.

Under `--debug`, the handler *re-raises* the exception. The
developer sees the full traceback. This is the *only* case where
`--debug` changes the behavior.

---

## Why `SystemExit`, not `typer.Exit`

The handler is called from `main()`, *after* Typer's dispatch has
finished. `typer.Exit` is only special *inside* a Typer command:
Typer catches it there and converts it to a `SystemExit` with the
right code. Outside Typer, `typer.Exit` is an ordinary exception that
propagates into `sys.exit(main())`, which prints a traceback and
exits 1.

The handler therefore raises `SystemExit` directly. It is the
exception `sys.exit` actually expects. The effect is identical to
Typer's conversion: the process exits with the correct code, no
traceback.

```
┌──────────────────────────────────────────────────────────────┐
│  INSIDE a Typer command:                                     │
│  ─────────────────────                                       │
│  raise typer.Exit(2)                                         │
│      → Typer catches it                                      │
│      → converts to SystemExit(2)                             │
│      → process exits 2                                       │
│                                                              │
│  OUTSIDE Typer (in main()):                                  │
│  ──────────────────────────                                  │
│  raise typer.Exit(2)                                         │
│      → NOT caught by Typer (dispatch is over)                │
│      → propagates to sys.exit(main())                        │
│      → sys.exit treats it as an uncaught exception           │
│      → prints traceback, exits 1                             │
│                                                              │
│  CORRECT:                                                    │
│  ────────                                                    │
│  raise SystemExit(2)                                         │
│      → sys.exit understands it                               │
│      → process exits 2, no traceback                         │
└──────────────────────────────────────────────────────────────┘
```

See [ADR 0004](../adr/0004-centralize-cli-output-and-error-handling.md)
for the full reasoning.

---

## What the handler never does

The handler's contract has five *negatives* — things it must never do,
regardless of the exception. Every one of them is tested:

### 1. Never emits a traceback for the user

`TestNoTracebackLeaks::test_no_traceback_in_output` runs 11
parametrized assertions, one per exception type. Every type must
produce output that does not contain `"Traceback"`.

### 2. Never emits a file path

A file path like `/home/user/project/src/forge/cli/main.py` is an
internal detail that could leak the user's home directory or project
structure. `TestNoTracebackLeaks::test_no_file_paths_in_output` runs
the same 11 assertions for `/src/`, `\src\`, and `/home/`.

### 3. Never emits the exception's class name

"RuntimeError" or "ValueError" are internal details. They are tested
in `TestUnexpectedException::test_does_not_leak_class_name`.

### 4. Never emits the exception's message

The message might contain sensitive data (a token, a database
connection string, a file path). It is tested in
`TestUnexpectedException::test_does_not_leak_exception_message`.

### 5. Never swallows an exception silently

Every path through the handler exits (via `SystemExit`) or re-raises
(under `--debug`). There is no "return None" path. This is tested
implicitly by every test that uses `pytest.raises(SystemExit)`.

---

## The exception hierarchy

`ForgeError` is the base class for every user-facing Forge error:

```
ForgeError (exit_code = INTERNAL_ERROR = 5)
├── UserInputError (exit_code = USER_ERROR = 1)
├── BlueprintError (exit_code = BLUEPRINT_ERROR = 2)
├── FilesystemConflictError (exit_code = FILESYSTEM_CONFLICT = 3)
└── VerificationError (exit_code = VERIFICATION_FAILED = 4)
```

Each subclass has a *distinct* exit code. This is the contract that
lets scripts branch on `$?` without parsing the message. Adding a
subclass is non-breaking; removing or renaming one is breaking. The
full table is in [../reference/exit-codes.md](../reference/exit-codes.md).

### The `hint` attribute

The `hint` attribute is a first-class part of the contract. An error
message without a hint is a dead end; a hint turns it into a
signpost. The handler renders it on a separate, dimmed line:

```
✗ project name is required
Hint: pass --name my-project
```

The hint is optional but *strongly encouraged*. See
[../guides/adding-an-exception.md](../guides/adding-an-exception.md)
for how to write a good one.

---

## How the handler interacts with the UI

The handler does not print. It delegates to `ui.py`'s helpers:

| Handler branch | Helper | Stream |
| :--- | :--- | :--- |
| `ForgeError` | `print_error` + `print_hint` | stderr |
| `click.ClickException` | `print_error` | stderr |
| Cancellation | `print_cancelled` | stderr |
| Unexpected exception | `print_error` + `print_hint` | stderr |

Every message from the handler goes to **stderr**, because every
message from the handler is commentary, not product. A script that
pipes `forge verify --json` to `jq` receives valid JSON on stdout
even when the command fails.

See [../reference/streams.md](../reference/streams.md) for the full
stream contract.

---

## The classification decision tree

```
                        handle_exception(exc, debug)
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
            isinstance(exc, ForgeError)?   isinstance(exc, click.ClickException)?
                     │ Yes                         │ Yes
                     ▼                             ▼
         print_error(str(exc))              print_error(exc.format_message())
         if exc.hint:                       raise SystemExit(exc.exit_code)
             print_hint(exc.hint)                  │
         raise SystemExit(exc.exit_code)            │
                     │                             │
                     └──────────────┬──────────────┘
                                    │ No (for both)
                                    ▼
                     isinstance(exc, KeyboardInterrupt | typer.Abort)?
                                    │ Yes
                                    ▼
                            print_cancelled()
                            raise SystemExit(1)
                                    │
                                    │ No
                                    ▼
                              if debug:
                                  raise exc
                                    │
                                    ▼
                     print_error("An unexpected internal error occurred.")
                     print_hint("Run with --debug ...")
                     raise SystemExit(5)
```

The order is **load-bearing**:

- `ForgeError` is checked first because it is the most specific
  user-error type. If a `ForgeError` is also a `ClickException` (it
  is not, but future subclasses might be), we want the ForgeError
  branch to win.
- `click.ClickException` is checked second because it is the second
  most specific user-error type.
- The cancellation branch is third because `KeyboardInterrupt` and
  `typer.Abort` are not user errors — they are cancellations.
- The unexpected branch is last because it is the catch-all.

**`if debug: raise exc` is placed *after* the user-error branches
and *before* the unexpected rendering.** That is what makes `--debug`
selective: user errors and cancellations never reach the `raise exc`
line.

---

## How to extend the handler

If you need a new category (for example, a category for rate-limit
errors that should exit with a different code), the process is:

1. **Identify the exception type** that represents the category. If
   it's a subclass of an existing type, you may not need a new branch
   — the existing branch will handle it.
2. **Add a new branch *before* the unexpected branch.** The branch
   must render a friendly message (no traceback) and raise
   `SystemExit(code)`.
3. **Add tests** to `tests/unit/cli/test_errors.py`: one test class
   for the category, with tests for the happy path, the `--debug`
   path (if the category should be treated differently under
   `--debug`), the stream, and the exit code.
4. **Add the exception type to `TestNoTracebackLeaks`** if it is a
   new *kind* of exception (not a subclass of an existing kind).
5. **Update this document** and
   [ADR 0004](../adr/0004-centralize-cli-output-and-error-handling.md)
   with the new category.

Do **not** add a new category for a failure that is already covered
by an existing category. If a new failure is a user error, it should
be a `ForgeError` subclass (see
[../guides/adding-an-exception.md](../guides/adding-an-exception.md)).
If it is an unexpected failure, it should fall through to the
unexpected branch. The categories are *categories*, not a growing
list of every exception type.

---

## The tests

The contract is proven by 49 tests in
`tests/unit/cli/test_errors.py`. They are grouped by category:

| Test class | Tests | What it proves |
| :--- | :--- | :--- |
| `TestForgeError` | 5 | Message + hint, exit code, stderr routing, conditional hint rendering. |
| `TestClickException` | 5 | `UsageError` and `BadParameter` render friendly, exit 2, stderr, no traceback. |
| `TestCancellation` | 4 | `KeyboardInterrupt` and `typer.Abort` render "Cancelled.", exit 1, no "Error:" prefix, no traceback. |
| `TestUnexpectedException` | 6 | Generic message, no class name, no exception message, no traceback, suggests `--debug`, points at issue tracker. |
| `TestDebugMode` | 5 | `--debug` re-raises only unexpected exceptions. User errors and cancellations are still rendered friendly. |
| `TestNoTracebackLeaks` | 22 | 11 exception types × 2 assertions: no `Traceback`, no file paths. |
| `TestExitCodeMapping` | 2 | All `ForgeError` subclasses have distinct codes; the base class uses `INTERNAL_ERROR`. |

If you change any behavior of the handler, one or more of these tests
must change. If you are changing a test without changing the
behavior, ask why — the test is the *specification*.

---

## Further reading

- [cli-architecture.md](cli-architecture.md) — the CLI layer in detail
- [overview.md](overview.md) — the whole codebase
- [../guides/adding-an-exception.md](../guides/adding-an-exception.md)
  — how to add a new `ForgeError` subclass
- [../reference/exit-codes.md](../reference/exit-codes.md) — the
  public exit-code contract
- [../reference/streams.md](../reference/streams.md) — the
  stdout/stderr contract
- [ADR 0004](../adr/0004-centralize-cli-output-and-error-handling.md)
  — the reasoning
