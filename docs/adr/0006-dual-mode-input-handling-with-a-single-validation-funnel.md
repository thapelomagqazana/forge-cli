# 0006. Dual-Mode Input Handling with a Single Validation Funnel

- **Status:** Accepted
- **Date:** 2026-11-07
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

`forge create` is the primary entry point for project generation. It
is used by two very different kinds of callers:

1. **A human at a terminal.** They have a blueprint ID in mind, but
   often do not yet know the project name, the destination directory,
   or the values for the blueprint's variables. They want the CLI to
   *ask* them, one question at a time, with sensible defaults and
   immediate feedback when an answer is wrong.

2. **A script or CI job.** It knows every value it needs and passes
   them as flags. It cannot answer a prompt, it has no TTY, and it
   branches on the exit code (`$?`) to decide what to do next. It
   expects missing inputs to fail *immediately*, with a message that
   names exactly what to add, and with a documented exit code.

Before this decision, the branch did not distinguish between these
callers. `forge create` required a `--name` flag unconditionally and
had no way to collect values interactively. A human running it was
greeted with a Click usage error for a flag they might not have known
they needed; a script was never exposed to the wizard because there
was none.

Two failure modes were observed in practice:

- **The "helpful wizard" failure.** A naive implementation asks for
  values interactively *and* accepts them as flags, then runs
  validation twice — once inline, once on the collected dict. The two
  validators drift. Six months later, `--name` rejects a name the
  wizard accepts, and the two paths produce different projects from
  the same input. This is the failure mode the design exists to
  prevent.

- **The "CI hang" failure.** A naive implementation asks for a value
  interactively *unconditionally*. A CI job invokes `forge create
  python.fastapi.api --no-interactive --name foo`, the wizard fires
  anyway because the flag is checked *after* the prompt, and the CI
  job hangs on stdin that will never receive a line. This is the
  failure mode that makes a CLI feel untrustworthy in automation.

The design must satisfy both callers without either failure mode.
Both must reach the same validation and the same exit codes; neither
may have a code path that the other cannot exercise in tests. The
constraint is not "add a wizard" — it is "add a wizard without adding
a second source of truth for what makes an input valid."

## Decision

We will **read the interactivity mode once per invocation, dispatch
to one of two input collectors, and funnel both collectors through a
single validation function**.

Specifically:

1. **Mode detection is a single function call, made once.** The
   command calls `prompts.is_interactive() and not --no-interactive`
   at the top of its input-resolution function. The result is not
   cached, re-checked, or threaded through the code. The flag
   overrides the TTY check (a script that wants a wizard can pass
   `--no-interactive` to force it off, and a user in a terminal who
   wants scripted behavior passes the same flag).

2. **The non-interactive path fails fast, with aggregation.** If the
   mode is non-interactive and a required value is missing, the
   command raises `MissingRequiredFlagsError` listing *every* missing
   flag in one message, before any side effects. The exit code is 1
   (`USER_ERROR`), per ADR-0005.

3. **The interactive path prompts for missing values only.** Flags
   win over prompts. A value supplied on the command line is not
   re-asked. A value not supplied is collected via `prompts.ask_text`
   / `ask_select` / `ask_confirm`, with the inline validator attached
   to the prompt for real-time feedback. Cancellation at any prompt
   raises `PromptCancelledError`, which the CLI handler renders as a
   dimmed "Cancelled." line on stderr and exits 1 — never as a
   traceback.

4. **Both paths converge on `handlers.validate_blueprint_inputs`.**
   This function is the single source of truth for what makes an
   input valid. It is *pure* (no filesystem access, no blueprint
   lookup, no rendering) so that it can be tested without `tmp_path`,
   reused from a future SDK entry point, and reasoned about in
   isolation. Per-variable rules are the blueprint's concern, not the
   funnel's; the funnel validates *shape* (non-empty, legal
   characters), not *content* (which values are legal for a specific
   variable).

5. **The command never prints and never catches.** Every user-facing
   exception is a `ForgeError` subclass, raised to `handle_exception`
   in `main()`. The handler is the single renderer; the command
   raises and lets the exception propagate. This is what makes the
   exit code and stream contract auditable in one place.

The **single-command module shape** is part of the decision, because
it is the shape the mode dispatch relies on: a module that wants a
wizard must expose a `typer.Typer` with a `@callback()` and no
subcommands. The registry detects this shape (`_is_single_command_app`)
and registers the callback as a leaf command under the module's name.
A module that registers subcommands via `@app.command()` is treated
as a multi-command group and gets the group-shaped registration
instead. This is the mechanism that makes `forge create <blueprint>`
parse as a single command with a positional argument, rather than
requiring `forge create create <blueprint>`.

## Consequences

### Positive

- **One rulebook, two collectors.** Adding a validation rule means
  editing `validate_blueprint_inputs`, not two call sites. The two
  modes cannot drift because there is only one mode-independent place
  for rules to live. The invariant is enforced by
  `test_create_dual_mode.TestInlineValidator.test_inline_validator_is_a_subset_of_authoritative_validator`.

- **The wizard is replaceable.** `prompts.py` exposes three prompt
  primitives (`ask_text`, `ask_select`, `ask_confirm`) and an
  interactivity check (`is_interactive`). The wizard's implementation
  can move to `rich.prompt`, `textual`, or a future web UI's prompt
  library without touching the command layer, because the command
  layer only knows the five-name contract.

- **The non-interactive path is scriptable and testable.** A CI job
  that gets a `MissingRequiredFlagsError` receives the complete list
  of what to add, in one message, before any side effects. The exit
  code (1) is documented and stable per ADR-0005.

- **Cancellation is uniform.** Ctrl+C at the first prompt, the last
  prompt, or any prompt in between produces the same behavior: a
  clean exit, a dimmed "Cancelled." line, and no traceback. Users
  who know that cancelling is safe will cancel; users who learn that
  cancelling is risky will Ctrl+Z their shell and file a bug.

- **The command layer is testable in isolation.** Every path through
  `_resolve_inputs` — missing flags, prompts, cancellation, unknown
  blueprint, inline validation — is exercised by tests that call
  `_resolve_inputs` directly, with a faked `prompts` module. The
  wizard's behavior is tested without a TTY.

### Negative

- **The wizard is not stateful.** There is no "back" button, no
  resume-from-session, no way to change an answer without restarting.
  A user who gets halfway through and realizes they typed the wrong
  project name must cancel and restart. This is deliberate — a
  4-question wizard does not warrant the complexity of state
  management — but it is a real limitation for a future, longer
  wizard.

- **`is_interactive()` is called once and trusted.** A command that
  later wants to *re-enable* the wizard after disabling it (e.g., a
  `--force-interactive` flag) cannot do so without re-reading the
  mode. This is a deliberate constraint: a mode that changes
  mid-invocation is a mode that is not auditable. If a future command
  needs mode changes, that is a new decision, not an extension of
  this one.

- **The inline validator duplicates the funnel's rules.** This is
  the *only* duplication in the design, and it is deliberate — it
  exists so the user gets feedback while typing, not after pressing
  Enter. The duplication is bounded by a test that asserts the inline
  validator accepts a strict subset of what the funnel accepts. A
  change to one without the other fails CI. The cost is that adding
  a rule requires editing two places; the benefit is that the two
  places cannot silently disagree.

- **The registry's single-command detection is not obvious.** A
  contributor who expects `add_typer` to work for a single-command
  app will discover, via a `Missing argument` error, that it does
  not. The pattern (`root_app.command(name)(app.registered_callback.callback)`)
  is documented in `registry._register_single_command` and in the
  architecture doc, but it is not the "obvious" call. This is a cost
  paid once per new command author, mitigated by the docs.

### Follow-up actions

- [x] Implement `prompts.py` with the five-name contract and uniform
      cancellation (`_guard`).
- [x] Implement `validate_blueprint_inputs` in `verification/handlers/`.
- [x] Add `MissingRequiredFlagsError` and `PromptCancelledError` to
      `core/exceptions.py`.
- [x] Wire the mode dispatch into `create.py::_resolve_inputs`.
- [x] Add single-command detection and registration to `registry.py`.
- [x] Add `_project_name_validator` as the inline UX affordance.
- [x] Document the invariants in `docs/architecture/prompt-flows.md`.
- [ ] **Blueprint loader.** `forge.blueprints.load_index` is not yet
      implemented, so every invocation that needs a real blueprint
      fails with exit 5. The contract tests are `xfail(strict=True)`
      against the current exit-5 behavior; when the loader lands,
      the xfail tests will fail and prompt an update to assert exit 2.
- [ ] **`--output` prompt suppression.** The wizard currently asks
      for destination even when `--output` was passed explicitly,
      because the option's default (`Path(".")`) is indistinguishable
      from an explicit flag at the point the wizard checks. A small
      cleanup task will thread the flag's `is_default` state through.
- [ ] **Variable resolver.** The wizard prompts for each blueprint
      variable's value, but the values are currently passed through
      unvalidated (the funnel accepts them opaquely). A future task
      validates them against the blueprint's declared schemas.

## Alternatives Considered

### Alternative 1: Two separate commands, `create` and `create-wizard`

The simplest split. `forge create` is the flag-driven command;
`forge create-wizard` is the interactive command. No mode detection,
no branch inside the command body — the user picks the entry point.

**Genuine merit.** This is the pattern many well-designed CLIs use
(`git commit` vs. `git commit --interactive` is a counter-example,
but `gh pr create` and similar use separate subcommands for interactive
and non-interactive flows). It is easy to discover, easy to document,
and easy to test — each command is one mode.

**Reason for rejection.** It duplicates the argument surface. Every
flag on `create` must exist on `create-wizard` (and vice versa) —
either by duplicating the Typer decorators or by factoring a shared
argument spec, which is a small abstraction that grows. It also
creates a discoverability problem: a user who runs `forge create`
with no flags sees "missing --name" and must *know* that
`create-wizard` exists to get a prompt instead. The single-command
design ("run `create` and it figures out what you need") is a better
UX for a command with 4 inputs; two commands would be right if the
surface were larger.

### Alternative 2: Always prompt, in every mode

The simplest implementation. The wizard runs unconditionally; flags
are treated as pre-filled answers that the wizard skips.

**Genuine merit.** A single code path is easier to maintain than two.
The "flags win" rule already suppresses prompts for known values, so
the wizard is fast for a user who has supplied everything. A CI job
that supplies every flag never sees a prompt — because the wizard
skips every prompt when the value is known.

**Reason for rejection.** The CI hang is the killer. If the wizard
runs unconditionally, a CI job that *forgets* one flag — a
`--no-interactive` typo, an env var that was supposed to supply the
name but did not — hangs on stdin forever. A hang is a worse failure
mode than an error: it has no exit code, no message, no diagnostic.
The user sees a stuck pipeline and has no way to know what went
wrong. The "fail fast, fail with a list" behavior of non-interactive
mode is what makes `forge create` safe in automation, and it is
unconditional.

Additionally, "always prompt" makes `forge create` unusable in
environments where stdin is not a TTY but *is* open (e.g., a shell
with a heredoc). The wizard would try to read from the heredoc,
misinterpret the first line, and fail confusingly. Detecting the
TTY is what tells us "there is genuinely no user here."

### Alternative 3: Mode detection via an environment variable

Instead of `is_interactive()`, use `FORGE_INTERACTIVE=1` (or
`CI=true`, or `TERM`) as the mode switch.

**Genuine merit.** Environment variables are explicit and scriptable.
A user who wants a wizard from a shell script can set the variable;
a CI job that wants flag-only behavior can set it too. The mode is
visible in the process's environment, which is easier to debug than
a runtime TTY check.

**Reason for rejection.** Environment variables must be *set*, and
the default is the question. If the default is interactive, CI jobs
that do not set `CI=true` hang (the same failure as Alternative 2,
just shifted to the environment). If the default is non-interactive,
a user at a terminal who did not set the variable gets a flag error
instead of a prompt (the UX regression). The TTY check is the one
signal that is set by the *system*, not by the user — a real
terminal has a TTY, a pipe does not — and it defaults correctly
in every case.

The `--no-interactive` flag exists as an override for the edge cases
(a terminal user who wants scripted behavior, a script that
inadvertently has a TTY). The env var would have been a *second*
override, and two overrides on one signal is one too many.

### Alternative 4: The wizard writes its answers to a file, and the command reads it

The wizard is a separate binary (`forge create-wizard`) that writes
`./.forge/create-answers.json`. `forge create` reads that file if
it exists.

**Genuine merit.** The wizard and the command are completely
decoupled. The answers are auditable, scriptable, and resumable. A
future UI (web, TUI) can produce the same file.

**Reason for rejection.** This is a bigger design than the task
warrants. The answers file is a public contract (its schema, its
version, its migration story) — it has to be tested, documented, and
supported. It also introduces a failure mode: a stale answers file
from a previous wizard run silently supplies wrong values to a
fresh command. The state-management cost is not paid by the
task's budget; the dual-mode design keeps state in memory and
discards it on any non-zero exit, which is the correct behavior for
a wizard whose job is to collect 4 values and stop.

If a future task *does* need cross-process state (say, a script that
collects answers in one step and applies them in another), it should
be a new decision with its own ADR, not an extension of this one.

## References

- `src/forge/cli/commands/create.py` — the reference implementation
  (mode dispatch, missing-flag aggregation, wizard prompts).
- `src/forge/cli/prompts.py` — the five-name prompt engine contract.
- `src/forge/verification/handlers/__init__.py` — the validation
  funnel.
- `src/forge/cli/registry.py::_is_single_command_app` and
  `_register_single_command` — the leaf-command registration that
  makes `forge create <blueprint>` parse correctly.
- `src/forge/core/exceptions.py` — `MissingRequiredFlagsError`,
  `PromptCancelledError`, and the exit-code mapping.
- `src/forge/cli/errors.py` — the exception handler and its
  cancellation-first dispatch.
- `docs/architecture/prompt-flows.md` — the invariants, the flow
  diagram, and the extension checklist.
- `tests/unit/cli/test_create_dual_mode.py`,
  `tests/unit/cli/test_create_interactive.py`,
  `tests/contract/test_cli_create_contract.py` — the three test
  layers for the two modes.
- ADR-0005: [Stabilize Exit Codes as a Public Contract](./0005-stabilize-exit-codes-as-a-public-contract.md)
- ADR-0004: [Centralize CLI Output and Error Handling](./0004-centralize-cli-output-and-error-handling.md)
