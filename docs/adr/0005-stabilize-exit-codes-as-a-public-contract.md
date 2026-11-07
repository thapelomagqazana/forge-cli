# 0005. Stabilize exit codes as a public contract

- **Status:** Accepted
- **Date:** 2026-11-07
- **Deciders:** Thapelo Magqazana
- **Consulted:** —
- **Informed:** Forge CLI contributors

## Context

Forge CLI exits with one of six codes. A script that runs
`forge create` and branches on `$?` is depending on the *meaning*
of each code. A change to a code is a breaking change — not because
the code is different, but because the *promise* is different.

Before this decision, the exit codes were scattered across three
places, each written for a different audience:

- **The code** — `src/forge/core/exceptions.py` had the `ExitCode`
  enum and the `ForgeError` hierarchy. The enum's module docstring
  listed the six codes. Each subclass had a docstring, but the
  docstrings were inconsistent (one had an "Examples" section, one
  did not; one explained the remediation, one did not).
- **The docs** — `docs/reference/exit-codes.md` was the external
  contract, written for script authors. It had the six codes,
  concrete examples, and branching snippets. It was correct, but it
  did not cross-reference the code or the tests.
- **The tests** — `tests/unit/cli/test_errors.py` had
  `TestExitCodeMapping` (proving each subclass has a distinct code)
  and `TestForgeError::test_each_subclass_uses_its_declared_exit_code`
  (proving each subclass maps to its code). But the test *names*
  did not match the WBS's acceptance criteria, so a reader
  checking "criterion 2" could not find the test by name.

The task description (1.1.5) said:

1. "Create an `ExitCode` `IntEnum` in `src/forge/cli/exit_codes.py`."
2. "Update the top-level exception handler to map each `ForgeError`
   subclass to the corresponding exit code."
3. "Use `typer.Exit(code=...)` to exit with the correct code."

Items 2 and 3 were **already done** in the previous task (1.1.4).
But two of the instructions are **out of date** with respect to
the previous task's decisions:

- **The enum lives in `core/`, not `cli/`.** The task says
  `src/forge/cli/exit_codes.py`. The actual location is
  `src/forge/core/exceptions.py`. Moving the enum to `cli/` would
  require `core/` to import from `cli/` — a violation of the
  inward dependency rule.

- **The handler uses `SystemExit`, not `typer.Exit`.** The task
  says `typer.Exit(code=...)`. The actual implementation raises
  `SystemExit`. The previous task's tests prove why: `typer.Exit`
  is only special *inside* a Typer command, and the handler is
  called *outside* Typer's dispatch. Using `typer.Exit` caused the
  process to exit 1 with a traceback instead of the declared code.

The constraints on the solution are:

1. **The contract must be in the code** — a contributor reading
   the enum should see the full contract without opening a separate
   doc file.
2. **The contract must be in the docs** — a script author should
   see the full contract without reading the source code.
3. **The tests must match the acceptance criteria** — a reader
   checking "criterion 2" should find the test by name, not by
   scanning parametrized tests.
4. **The enum must stay in the domain** — moving it to `cli/`
   would violate the inward dependency rule.
5. **The handler must use `SystemExit`** — `typer.Exit` is wrong
   for the handler's context.

## Decision

We will **stabilize the exit codes as a public contract** by
consolidating the documentation in the code, the docs, and the
tests — **without** moving the enum or changing the exception type.

Specifically:

### 1. The code (`src/forge/core/exceptions.py`)

The module docstring now contains:

- The full exit-code table as an ASCII table (six codes, six
  meanings, one severity rule).
- A section explaining why the enum lives in `core/`, not `cli/`.
- A section explaining why the handler raises `SystemExit`, not
  `typer.Exit`.
- A stability statement listing the public API surface.

Each `ForgeError` subclass docstring is restructured into four
labelled paragraphs:

- **Exit code:** the code it declares, and the enum member it uses.
- **When raised:** the failures that produce this subclass.
- **What the user should do:** the remediation, in one sentence.
- **Examples:** concrete commands that would raise it.

The structure is deliberate: a reader can scan the four labels
without reading prose, and a contributor adding a new subclass has
a template to copy.

### 2. The docs (`docs/reference/exit-codes.md`)

The external contract is unchanged (it was already correct). The
ADR records that the doc file is the *authoritative* statement of
the contract — if the code and the doc ever disagree, the doc
wins, and the disagreement is a bug.

### 3. The tests (`tests/unit/cli/test_errors.py`)

A new test class, `TestExitCodeAcceptanceCriteria`, whose test
names match the WBS's acceptance criteria. Each criterion has a
*named* test:

- `test_user_input_error_returns_exit_code_1` (criterion 2)
- `test_filesystem_conflict_error_returns_exit_code_3` (criterion 3)
- `test_verification_error_returns_exit_code_4` (criterion 4)
- `test_unexpected_exception_returns_exit_code_5` (criterion 5)
- `test_blueprint_error_returns_exit_code_2` (criterion 1, extended)
- `test_forge_error_base_returns_exit_code_5` (criterion 1, extended)
- `test_every_subclass_has_a_documented_exit_code` (criterion 1)
- `test_all_exit_codes_are_distinct` (criterion 1)

The new class *duplicates* coverage from `TestForgeError` and
`TestUnexpectedException`. The duplication is intentional: the new
class is the *named* proof of each criterion, and a reader
checking the WBS does not have to scan the parametrized tests.

### 4. The ADR (this document)

This document exists because the WBS's instructions were out of
date. A future contributor who reads only the WBS (not this ADR)
will think the enum should be in `cli/` and the handler should use
`typer.Exit`. They will make those changes, break the dependency
direction and the exit-code contract, and the tests will fail. This
ADR records the decisions and the *reason* they differ from the
WBS.

## Consequences

### Positive

- **The contract is in one place per audience.** A contributor
  reading the enum sees the full table. A script author reading
  `docs/reference/exit-codes.md` sees the same table plus
  examples. A reader checking a specific acceptance criterion
  finds the test by name. No one has to search across three files.

- **The dependency direction is preserved.** The enum stays in
  `core/`, so `core/` does not import from `cli/`. The domain
  remains testable without the CLI, and reusable in a future web
  interface.

- **The exit-code contract is correct.** The handler raises
  `SystemExit`, so the process exits with the declared code, no
  traceback. This is proven by 57 tests in `test_errors.py`, all
  of which would fail if the handler reverted to `typer.Exit`.

- **The tests are readable as a contract.** A reader checking
  "criterion 2" finds `test_user_input_error_returns_exit_code_1`
  by name. The test *is* the criterion.

- **The WBS's discrepancies are documented.** A future contributor
  who reads the WBS and wonders why the enum is not in `cli/`
  finds the answer in this ADR.

### Negative

- **The test suite is longer.** `TestExitCodeAcceptanceCriteria`
  adds 8 tests that duplicate coverage from `TestForgeError` and
  `TestUnexpectedException`. This is intentional, but it is a
  *cost*: the test suite takes longer to run, and a future
  refactor might "helpfully" remove the duplication (defeating
  the purpose).

- **The ADR exists to correct the WBS.** The WBS is a *planning*
  artifact, and it is now out of date with respect to the
  implementation. A contributor who reads only the WBS will be
  misled. This ADR is the correction, but it only helps if the
  contributor reads it.

- **The stability policy is a commitment.** Every exit code is now
  a *public promise*. Changing one requires a major version bump,
  an ADR, a doc update, a test update, a changelog notice, and a
  migration path. This is the *correct* commitment for a
  developer tool, but it is a *cost*: the codes cannot be casually
  revised.

### Follow-up actions

- [x] Consolidate the exit-code docstrings in
      `core/exceptions.py` (the ASCII table, the "why the enum
      lives here" section, the "why `SystemExit`" section, the
      stability statement, the four-paragraph subclass docstrings).
- [x] Add the `TestExitCodeAcceptanceCriteria` class to
      `tests/unit/cli/test_errors.py`.
- [x] Write this ADR.
- [ ] Update the WBS to match reality: the enum lives in
      `core/`, the handler uses `SystemExit`, the tests are named
      after the acceptance criteria. (The WBS is a planning
      artifact, and the ADR is the code-level correction. Updating
      the WBS itself is *not* necessary for the code, but it is
      *useful* for future contributors who read the WBS. The
      decision to update the WBS is deferred to the planning
      process.)
- [ ] Add an import-linter hook to enforce the inward dependency
      rule. (The rule is currently enforced by code review, not
      by tooling. When the codebase grows, this becomes harder
      to review. The hook will be added when the codebase exceeds
      a manageable size, or when a violation occurs.)
- [ ] Add a CI check that compares the exit-code table in
      `core/exceptions.py` against the table in
      `docs/reference/exit-codes.md`. (The two tables are
      currently kept in sync manually. A check would catch a
      drift automatically.)

## Alternatives Considered

### Alternative 1: Move the enum to `src/forge/cli/exit_codes.py`

This is what the WBS says. The argument for it: exit codes are a
CLI concern, so they belong in the CLI layer.

**Why rejected:** The enum is used by `core/exceptions.py` (each
`ForgeError` subclass declares its `exit_code`). Moving it to
`cli/` would require `core/` to import from `cli/` — a violation of
the inward dependency rule. The domain would depend on the CLI,
which means:

1. The domain could not be tested without the CLI. Every test of
   `ForgeError` would need to import from `cli/`.
2. The domain could not be reused in a future web interface or
   library API. A web interface would have to depend on the CLI,
   not just on the domain.
3. The dependency direction would be inverted, which is the
   single most important structural decision in the architecture.

The exit code is a *domain* decision (what kind of failure is
this?), not a *CLI* decision (how should it be rendered?). The
decision belongs in `core/`; the rendering belongs in `cli/`. The
`cli/errors.py` classifier *reads* `exc.exit_code` and exits with
it — it does not *define* the codes.

### Alternative 2: Use `typer.Exit(code=...)` in the handler

This is what the WBS says. The argument for it: Typer's own
exceptions are the idiomatic way to exit from a Typer command.

**Why rejected:** The handler is called from `main()`, *outside*
Typer's dispatch. `typer.Exit` is only special *inside* a Typer
command — Typer catches it and converts it to a `SystemExit`.
Outside Typer, `typer.Exit` is an ordinary exception that
propagates into `sys.exit(main())`, which prints a traceback and
exits 1.

The previous task's tests prove this. Before the fix, the process
exited with a traceback and code 1, not the declared code. After
the fix (raising `SystemExit` directly), the process exits with
the correct code, no traceback. The tests
(`TestForgeError::test_each_subclass_uses_its_declared_exit_code`)
assert on `SystemExit.code`, which would fail if the handler
raised `typer.Exit`.

The `typer.Exit` alternative is *idiomatic* but *wrong for this
context*. The handler's context is "outside Typer", and the
correct exception in that context is `SystemExit`.

### Alternative 3: Duplicate the enum in both `cli/exit_codes.py` and `core/exceptions.py`

This is a compromise: put a copy of the enum in each layer, so
each layer "owns" its codes.

**Why rejected:** Two sources of truth is *worse* than one. When
the two enums disagree (which they eventually will), a script
author who reads one enum will be wrong about the other. The
duplication also creates a maintenance burden: every new code must
be added in two places, and the two additions must be kept in
sync. The single source of truth is `core/exceptions.py`; the CLI
*uses* it, does not *own a copy*.

### Alternative 4: Do nothing (the WBS's intent is already satisfied)

The intent — "stabilize the exit codes as a public contract" — is
already *functionally* satisfied: the enum exists, the handler
uses it, the tests pass, the docs exist. Doing nothing is a valid
option.

**Why rejected:** The intent is satisfied *functionally*, but the
contract is *scattered* across three places that do not
cross-reference each other. A contributor reading the enum does not
see the "why the enum lives here" explanation. A reader checking an
acceptance criterion does not find the test by name. A contributor
reading the WBS thinks the enum should be in `cli/` and the handler
should use `typer.Exit` — and there is no ADR to correct them.

The consolidation is *small* (documentation and test names, not
behaviour), but it makes the contract *readable* in one place per
audience. The cost is low; the benefit is high.

## References

- [`docs/reference/exit-codes.md`](../reference/exit-codes.md) —
  the external contract
- [`docs/architecture/error-handling.md`](../architecture/error-handling.md)
  — the internal classifier contract
- [`docs/adr/0004-centralize-cli-output-and-error-handling.md`](0004-centralize-cli-output-and-error-handling.md)
  — the classifier's design
- [`src/forge/core/exceptions.py`](../../src/forge/core/exceptions.py)
  — the code
- [`tests/unit/cli/test_errors.py`](../../tests/unit/cli/test_errors.py)
  — the tests
- [`docs/architecture/overview.md`](../architecture/overview.md) —
  the inward dependency rule and why it matters
