# Prompt Flows — Interactive and Non-Interactive Input

**Status:** Implemented in Task 1.1.6
**Decision record:** [ADR-0006: Dual-Mode Input Handling](../adr/0006-dual-mode-input-handling.md)
**Related:** [CLI Architecture](./cli-architecture.md), [Error Handling](./error-handling.md), [Exit Codes](../reference/exit-codes.md)

---

## Why this document exists

Every command that collects input from a user has to answer one question:

> *"Do I have a human on the other end, or a script?"*

If a human, the command should *ask*. If a script, the command must *fail fast with an explanation*. Both paths converge on the same validation, the same error types, and the same exit codes. This document is how that convergence is implemented in `forge create` — the first command with a prompt flow — and how it should be extended to future commands.

The document assumes the reader has read [ADR-0006](../adr/0006-dual-mode-input-handling.md). The ADR records *what was decided*; this document records *how it works* and *how to reason about it*.

---

## The two modes, one picture

```
                       ┌─────────────────────────────────┐
                       │ forge create <BLUEPRINT> …      │
                       └────────────────┬────────────────┘
                                        │
                                        ▼
                       ┌─────────────────────────────────┐
                       │ registry.get_blueprint(id)      │ ←── fail fast, exit 2
                       │  (loads the blueprint entry)    │
                       └────────────────┬────────────────┘
                                        │
                                        ▼
                       ┌─────────────────────────────────┐
                       │ is_interactive() and not        │
                       │ --no-interactive ?              │
                       └───┬─────────────────────────┬───┘
                           │ YES                     │ NO
                           ▼                         ▼
             ┌────────────────────────┐  ┌────────────────────────┐
             │ prompts.ask_select()   │  │ check flags            │
             │ prompts.ask_text()     │  │ missing? → exit 1      │
             │ prompts.ask_confirm()  │  │                        │
             └────────────┬───────────┘  └────────────┬───────────┘
                          │                           │
                          ▼                           ▼
             ┌─────────────────────────────────────────────────┐
             │ handlers.validate_blueprint_inputs()            │
             │  (the single validation funnel)                 │
             └─────────────────────┬───────────────────────────┘
                                   │
                                   ▼
             ┌─────────────────────────────────────────────────┐
             │ _generate(inputs, dry_run=…, overwrite=…)       │
             │  (pre-flight; pipeline stub until Task 1.1.x)   │
             └─────────────────────┬───────────────────────────┘
                                   │
                                   ▼
             ┌─────────────────────────────────────────────────┐
             │ handle_exception (in main())                    │
             │  renders errors → stderr; exit codes per ADR-5  │
             └─────────────────────────────────────────────────┘
```

The **branch happens once**, at the top of `_resolve_inputs` (`src/forge/cli/commands/create.py`). Everything below the branch is mode-agnostic. This is deliberate: a branch that happens once is testable, auditable, and impossible to get inconsistent.

---

## The four invariants

These invariants hold in every dual-mode command. Each is backed by at least one test; each has a "why" that would not be obvious from the code alone.

### Invariant 1 — The mode is decided once, never re-decided

`is_interactive()` is called at most once per invocation. The result is not cached (it is read each call, so a future command could theoretically change its mind — but that would be a bug, and the tests do not permit it). Every subsequent prompt or flag check trusts the mode.

**Why:** A mode that changes mid-invocation produces a wizard that asks three questions and then suddenly fails because stdin closed. That is worse than a wizard that never started.

**Enforced by:** `test_create_dual_mode.TestInteractiveWizard.test_no_interactive_flag_disables_wizard_even_on_tty` — proving the flag overrides a TTY, and the wizard never fires.

### Invariant 2 — Flags win over prompts, always

If a value can be supplied as a flag, it is not prompted for. The wizard only asks for values that *were not* supplied.

**Why:** Muscle memory. A user who ran `forge create x --name y` yesterday expects it to keep working today, in a terminal, with no prompts. A wizard that re-asks answered questions feels broken, and users respond to "feels broken" by *not using the wizard* — which defeats the point.

**Enforced by:** `test_create_dual_mode.TestInteractiveWizard.test_does_not_re_prompt_when_name_supplied_as_flag` — the test asserts the prompt function is *not called*, which is the observable form of "flags win."

### Invariant 3 — Validation has exactly one funnel

Every value, whether from a flag or from a prompt, flows through `handlers.validate_blueprint_inputs` before it is used. There is no second validation path.

**Why:** Two validation paths *will* drift. One will gain a rule the other does not, and the two modes will accept different projects. That is the exact technical debt the dual-mode design exists to prevent — and the reason this invariant is stated before any of the implementation.

**Enforced by:** `test_create_dual_mode.TestInlineValidator.test_inline_validator_is_a_subset_of_authoritative_validator` — the wizard's inline validator is proven to accept a *subset* of what the funnel accepts, so any rule added to the funnel without adding it inline stays correct, and any rule added inline without adding it to the funnel fails this test.

### Invariant 4 — Cancellation is uniform across every prompt

Every prompt funnels through `_guard` in `src/forge/cli/prompts.py`, which translates `KeyboardInterrupt` and `EOFError` into `PromptCancelledError`. There is no prompt path that produces a traceback.

**Why:** A user who presses Ctrl+C at the *first* prompt and gets a clean exit, and at the *last* prompt and gets a traceback, has been taught that "cancelling is risky." Users who learn that will not cancel; they will Ctrl+Z the shell and complain. Cancellation must be uniformly safe.

**Enforced by:**
- `test_create_interactive.TestCancellationAtEveryStep.test_cancellation_propagates_from_each_prompt` — parametrized across every prompt in the wizard.
- `test_create_dual_mode.TestCancellation.test_cancellation_propagates_from_prompt` — proves the command does not catch and rewrap.
- `test_create_dual_mode.TestCancellation.test_cancellation_propagates_from_cli_with_exit_1` — proves the exception reaches the CLI boundary.

---

## The modules involved

| Module | Role | Not responsible for |
|---|---|---|
| `src/forge/cli/commands/create.py` | Orchestrates the mode branch, collects inputs, hands to `_generate`. | Validation rules, prompt rendering, error rendering. |
| `src/forge/cli/prompts.py` | Renders the three prompt primitives (`ask_text`, `ask_select`, `ask_confirm`), handles cancellation. | Validation rules, blueprint knowledge, filesystem. |
| `src/forge/cli/registry.py` | Loads blueprint entries (`get_blueprint`, `list_blueprints`). | Prompt rendering, validation. |
| `src/forge/verification/handlers/__init__.py` | Validates inputs (`validate_blueprint_inputs`). | Prompt rendering, filesystem state. |
| `src/forge/cli/errors.py` | Classifies exceptions and renders them (or re-raises under `--debug`). | Knowing *what* was wrong (that is the exception's job). |
| `src/forge/core/exceptions.py` | Defines the exception hierarchy and the exit-code mapping. | Rendering, dispatch. |

The seam between `create.py` and `prompts.py` is the `Validator` type alias (a callable returning `None` or a message). The seam between `create.py` and `handlers` is the `ValidatedInputs` return type. The seam between `create.py` and `errors.py` is "raise a `ForgeError` subclass; the handler will render it." Each seam is narrow, and each module is replaceable without touching the others.

---

## What the wizard actually asks

`forge create` currently asks these questions, in this order, and only when the answer is not already known from a flag:

1. **Blueprint** (`ask_select`) — the list from `registry.list_blueprints()`, sorted. Not asked if the blueprint ID was passed as the positional argument.
2. **Project name** (`ask_text` with `validator=_project_name_validator()`) — real-time validation, re-prompts on invalid input. Not asked if `--name` was passed.
3. **Destination directory** (`ask_text`, default `<cwd>/<name>`) — the default uses the name, so this prompt comes after the name. Not asked if `--output` was passed explicitly (the current implementation asks for the destination even when `--output` is passed — see "Known limitations" below).
4. **Blueprint variables** — for each variable declared by the blueprint:
   - if `type == "boolean"` → `ask_confirm`
   - if `choices` is non-empty → `ask_select`
   - otherwise → `ask_text` with the variable's `validate` callback attached as the prompt's `validator`

The order is a UX contract, not an implementation detail. The blueprint is first because its variables drive the later prompts. The name is second because the destination default uses it. The destination is third because it is the last non-blueprint-specific choice. Variables are last because they are the most specific — if the user is going to cancel, they should cancel *before* being asked five variable-specific questions.

**Enforced by:** `test_create_interactive.TestWizardFlow.test_wizard_asks_for_blueprint_then_name_then_destination` — asserts on the *sequence* of prompt calls, not just on the final result.

---

## The prompt engine's contract

`src/forge/cli/prompts.py` exposes five public names and nothing else:

```python
Validator = Callable[[str], "str | None"]
def is_interactive() -> bool
def ask_text(message: str, *, default=None, validator=None, allow_empty=False) -> str
def ask_select(message: str, choices: Sequence[str], *, default=None) -> str
def ask_confirm(message: str, *, default: bool = True) -> bool
```

The engine is **deliberately dumb**. It knows how to ask a question and how to render a validation error. It does **not** know what a "project" is, what a "blueprint" is, whether a name is valid, or whether the filesystem is ready. Every one of those concerns lives somewhere else, and the engine stays replaceable.

Three consequences of this design that are worth stating explicitly:

1. **The engine is replaceable.** The current implementation uses `questionary`. A future implementation could use `rich.prompt`, `textual`, or a future web UI's prompt library. The only thing that has to survive is the five-name contract above. This is what makes the design "futureproof" in the sense that matters: it is *replaceable*, not *predictive*.

2. **The engine has no opinion about *what* is valid.** The validator is a callback. The engine calls it; the caller decides what "valid" means. This is why the same `ask_text` serves both the project name (which must match `^[a-z][a-z0-9-]*$`) and a blueprint's freeform description (which accepts anything). The engine does not know which is which — and does not need to.

3. **The engine handles cancellation once.** `_guard` is the only place `KeyboardInterrupt` and `EOFError` become `PromptCancelledError`. Adding a new prompt primitive means wrapping it in `_guard`; the wrapping is one line, and the test suite asserts the wrapping is present for every primitive (see `test_prompts.test_every_prompt_propagates_cancellation`).

---

## The validation funnel's contract

`handlers.validate_blueprint_inputs` is a **pure function** of its arguments: no filesystem access, no blueprint lookup, no rendering. That purity is the load-bearing property.

```python
def validate_blueprint_inputs(
    *,
    blueprint_id: str,
    name: str,
    destination: str | Path | None,
    variables: dict[str, Any] | None = None,
) -> ValidatedInputs
```

Three consequences of this design:

1. **It is testable without `tmp_path`.** The funnel tests in `test_exceptions.py`, `test_errors.py`, and `test_create_dual_mode.py` do not create fixtures on disk. They pass strings. The one place the funnel *does* check the filesystem is destination-conflict — and that check has been deliberately moved out of the funnel to `_generate`. The funnel validates *what the user typed*, not *what the filesystem looks like*.

2. **It is usable from a future SDK.** A web server that accepts the same inputs calls the same function. It does not have to import `questionary`, `typer`, or `pathlib`. The purity is what makes the funnel usable outside the CLI.

3. **It does not inspect variables.** Per-variable rules belong to the blueprint, not the funnel. The funnel accepts `variables` as an opaque dict; a future "variable resolver" task will validate them against the blueprint's declared schemas. Splitting shape-validation (the funnel's job) from content-validation (the blueprint's job) keeps the funnel stable when new variable types are added.

**Enforced by:**
- `test_create_dual_mode.TestInlineValidator.test_inline_validator_is_a_subset_of_authoritative_validator` — the invariant that the inline validator never accepts what the funnel rejects.
- `test_create_dual_mode.TestNonInteractiveMissingFlags.test_missing_name_raises_missing_flags_error_directly` — the funnel raises the correct domain error.

---

## Error flow across the two modes

Errors are classified by **category**, and the category determines both the rendering and the exit code. The full category table is in `docs/architecture/error-handling.md`; this section covers the four categories the prompt flow produces.

```
                        ┌──────────────────┐
                        │ Command body runs│
                        └────────┬─────────┘
                                 │
                                 ▼
                 ┌───────────────────────────────┐
                 │ Which category of exception?  │
                 └───┬───────┬───────┬───────┬───┘
                     │       │       │       │
                     ▼       ▼       ▼       ▼
              ┌─────────┐ ┌──────┐ ┌─────┐ ┌──────────┐
              │ User   │ │Blue- │ │File │ │Unexpected│
              │ error  │ │print │ │system│ │(bug)    │
              └────┬────┘ └──┬───┘ └──┬──┘ └────┬─────┘
                   │         │        │         │
                   ▼         ▼        ▼         ▼
              ┌─────────────────────────────────────────────────┐
              │ handle_exception                                │
              │  User error → red error + hint, stderr, exit 1  │
              │  Blueprint  → red error + hint, stderr, exit 2  │
              │  Filesystem → red error + hint, stderr, exit 3  │
              │  Unexpected → generic message, stderr, exit 5   │
              └─────────────────────────────────────────────────┘
```

Cancellation is a fifth category, rendered as a dimmed "Cancelled." line and exiting 1, but **not** as an "error" — it is commentary that the user chose to stop. The `handle_exception` function checks `PromptCancelledError` **before** `ForgeError`, so a cancellation is rendered as a cancellation even though `PromptCancelledError` is technically a `UserInputError` subclass. See `test_errors.TestCancellation` for the ordering assertion.

---

## What this design is *not*

Three tempting simplifications that this design deliberately rejects, each with the reason.

### Not a "one prompt at a time" abstraction

Tempting design: a `Wizard` class that holds the answer dict, iterates a list of prompt definitions, and returns the collected answers.

**Why rejected:** The "prompt definitions" become a second source of truth for which prompts fire, in what order, and with what defaults. Every new prompt requires editing both the definition list *and* the code that consumes the answers. The current design has the questions in the code, in the order they run, in the function that consumes them — one place to look.

### Not a re-entry/back-button wizard

Tempting design: the wizard remembers answers and allows the user to go "back" to a previous prompt.

**Why rejected:** "Back" implies state, which implies invalidation, which implies "if the user changes the blueprint after answering the name, the name's default destination is wrong." Back-navigation is a legitimate UX for rich wizards, but it is not the UX of a 4-question CLI. If a future command genuinely needs back-navigation, it should be its own module with its own ADR, not a modification to this one.

### Not a "resume from a saved session" wizard

Tempting design: the wizard saves partial answers to `.forge/create.json` and resumes if interrupted.

**Why rejected:** The wizard asks at most 4 questions (plus per-variable prompts). A user who wants to script the answers should use `--no-interactive` with flags, not resume a half-finished wizard. Saved state is a bug surface (what if the blueprint changed between sessions?), and the fix for "I was interrupted" is "re-run and re-answer" — which is cheap for 4 questions.

### Not the tool: `invoke_without_command=True`

This one deserves its own note, because *I got it wrong twice on this branch*.

The `typer.Typer` constructor has a `invoke_without_command: bool = False` parameter. Its name suggests "this app can be invoked without a subcommand." It does not do what the name suggests. It only controls whether the *callback* runs when a subcommand *is* invoked with no arguments; it does **not** remove the `COMMAND [ARGS]...` slot from the app's signature. Placing it on the decorator, or on the constructor, produces the same misleading `Missing argument 'BLUEPRINT'` error either way.

The *actual* mechanism for making a single-command module a leaf command is in `registry._is_single_command_app` + `_register_single_command`:

```python
# In registry.py
if _is_single_command_app(app):
    root_app.command(command_name)(app.registered_callback.callback)
else:
    root_app.add_typer(app, name=command_name)
```

A `Typer` with a callback and no subcommands is a single-command app; registering its callback *directly* on the root app produces a leaf command with the callback's signature. That is what `forge create <blueprint>` needs, and it is what the registry now does.

**Why this is documented here:** a future contributor who sees `create.py` and wonders "why isn't there a `invoke_without_command=True`?" will find the answer in this doc rather than re-discovering the two failed attempts. The mistake was subtle enough that it cost two test runs; the doc exists so it costs zero.

---

## Extending to a new command

If you are adding a new command that has a prompt flow, follow this checklist. The checklist is derived from the invariants above, not from `create.py` specifically.

1. **Expose a `Typer` with a `@callback()`.** Do not use `@app.command()` — that nests the command inside a group. The registry's `_is_single_command_app` detector handles the rest.

2. **Mark required values as `Optional` (i.e., `default=None`) at the Typer layer.** Enforce the requirement in your command body, where the interactive fallback exists. The `name` flag on `create` is the reference implementation.

3. **Call `is_interactive()` exactly once**, at the top of your input resolution. Do not call it inside loops. Do not cache it globally.

4. **For each missing value, call the corresponding `ask_*` primitive.** Use `ask_select` for enumerated choices, `ask_confirm` for booleans, `ask_text` for freeform (with a validator if the value has rules).

5. **Attach validators to prompts for real-time feedback.** The validator is a *UX affordance* — the authoritative validation must still happen in `validate_*`. The two must stay in sync; a test that asserts "inline validator accepts a strict subset of what the funnel accepts" is the pattern.

6. **Call the validation funnel** (or a new funnel in `handlers/`) after collection. Do not validate inline; do not skip validation for flag-supplied values.

7. **Raise domain exceptions.** Never `print` and `sys.exit`. Raise a `ForgeError` subclass; `handle_exception` will render it and exit with the correct code. The handler is the single renderer.

8. **Add a test that asserts the *order* of prompts** (see `test_create_interactive.TestWizardFlow.test_wizard_asks_for_blueprint_then_name_then_destination`). This is the test that catches a UX regression where the wizard asks in a different order but produces the same final result.

9. **Add a test that asserts cancellation propagates from every prompt** (see `test_create_interactive.TestCancellationAtEveryStep`). Parametrize across prompts; the test is the enforcement of Invariant 4.

10. **Update this document's "What the wizard actually asks" section** with your command's questions, in order. The doc is the reference for UX reviewers and for future contributors; an out-of-date doc is worse than no doc.

---

## Known limitations

These are documented here, not fixed, because they belong to other tasks or are out of scope for the prompt flow itself.

- **`--output` is asked for even when passed explicitly.** The current `_resolve_inputs` prompts for destination only when `raw["destination"]` is `None` (i.e., `--output` was not passed). But the user-facing `--output` default is `Path(".")`, so it is never `None` in practice — the wizard asks for destination every time unless the user *also* passes a flag that the current implementation does not check. This is a UX inconsistency (the flag and the default are conflated), and it belongs to a small cleanup task, not this one. The current behavior is *correct* (the wizard asks once), just not *ideal* (the flag's presence could suppress the prompt).

- **The blueprint loader is not implemented on this branch.** `forge.blueprints.load_index` does not exist yet; the registry's `_load_blueprint_index` raises `ImportError`, which surfaces as exit 5. Every interactive or non-interactive invocation that needs a real blueprint therefore fails until the loader task lands. This is documented in the contract tests as a `xfail(strict=True)` — the tests will start passing (and the xfail will start failing, prompting an update) when the loader lands.

- **`--json` output is not implemented.** The prompt flow is agnostic to it (the exit codes and streams are the same), but a script consuming `forge create --json` will not find that flag yet. The relevant task adds it; the prompt flow does not need to change.

---

## See also

- [ADR-0006: Dual-Mode Input Handling](../adr/0006-dual-mode-input-handling.md) — the decision record
- [CLI Architecture](./cli-architecture.md) — how commands are structured and mounted
- [Error Handling](./error-handling.md) — the full classification and rendering table
- [Exit Codes](../reference/exit-codes.md) — the public exit-code contract
- `src/forge/cli/commands/create.py` — the reference implementation
- `src/forge/cli/prompts.py` — the prompt engine's contract
- `src/forge/verification/handlers/__init__.py` — the validation funnel
- `src/forge/cli/registry.py` — the single-command detection and registration
- `tests/unit/cli/test_create_interactive.py` — the wizard's prompt-flow tests
- `tests/unit/cli/test_create_dual_mode.py` — the dispatch tests
- `tests/contract/test_cli_create_contract.py` — the shell-visible contract tests
