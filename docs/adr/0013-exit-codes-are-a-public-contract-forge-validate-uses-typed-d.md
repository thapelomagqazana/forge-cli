
Exit `2` and `5` do not produce a JSON envelope. They are the two
cases where "the tool did not perform a validation" — the only
two cases where the JSON contract for `forge validate --json`
correctly says "there is no envelope." This is documented in
`docs/reference/CLI_VALIDATE_OUTPUT.md` and guarded by tests.

6. **Automation branches on `$?` first, payload second.** A
well-written consumer never inspects the JSON without first
checking the exit code, because exit `2` and `5` legitimately
produce no JSON. This ordering is documented in the reference
doc and demonstrated in every "consumer recipe" example.

## Consequences

### Positive

- **One vocabulary across the CLI.** `forge list`, `forge validate`,
and every future command use the same `ExitCode` members for the
same reasons. A CI script written against `forge validate` today
will not need re-education when `forge create` adopts the same
convention — the "exit 1 means user error, exit 5 means report a
bug" rule is universal in the tool.

- **Distinguishable failure modes.** CI jobs can branch on `$?`
to distinguish "your blueprint has a problem" (exit 1, print the
errors) from "the tool has a problem" (exit 5, open a bug) from
"you called me wrong" (exit 2, fix the command line). Every one
of those was previously a `1`, and every one produced a different
remediation.

- **Bug visibility.** Because exit `5` is uniquely reserved for
Forge bugs, a bug that would previously have been silently
rendered as "your blueprint is invalid" now surfaces as exit 5
with a message pointing at the issue tracker. That is a
one-way door on a class of "user sees wrong error, files
confusing bug report" interactions.

- **Contract testability.** `ExitCode` is a four-line type with a
documented mapping. Tests can assert
`result == ExitCode.USER_ERROR` rather than `result == 1` — the
intent is named, and a future renumbering fails every test that
hard-coded the number instead of the enum member. That is
exactly the review friction we want on a contract change.

- **Machine-readable contract matches the JSON contract.** Both
the exit code and the JSON envelope are documented in
`docs/reference/`, both are versioned, and both are locked by
contract tests (`test_validate_output_contract.py`,
`test_validate_exit_codes.py`). A change to either is a
three-file change (code, doc, fixture) that a reviewer cannot
miss.

### Negative

- **Contribution friction.** A new command author must consult the
`ExitCode` table and map their outcomes correctly, rather than
returning `1` for everything and letting the caller figure it
out. That is a fifteen-second cost per command, paid once, in
exchange for a class of consumer bugs that we never write again.
That trade is worth it, but it is real.

- **Two places to update on a new outcome.** Adding a sixth "user
can fix it" outcome requires updating: (a) the `ExitCode` enum,
(b) the exit-code table in `docs/reference/exit-codes.md`, and
(c) any command that can produce it. Three files for one outcome
is heavier than "return 6 in a fit of inspiration," which is
precisely the point: a renumbering of the enum is a breaking
change, and three coordinated edits is the minimum ceremony for
a breaking change.

- **`ExitCode` renumbering is a major version.** Because the enum
is `IntEnum`, changing `USER_ERROR = 1` to `USER_ERROR = 7`
silently changes the process status for every consumer, with no
code-level signal. The enum's ints are load-bearing; treat
renumbering as a breaking change in the changelog.

- **The `2` for "usage error" conflicts with argparse.** Python's
`argparse` uses `2` for its own usage errors. Typer inherits
that behaviour. Our `ExitCode.BLUEPRINT_ERROR` is also `2`. The
numeric collision is intentional — "the invocation is wrong" is
the same category for a user whether the cause is a missing
argument or a missing manifest — but the *name* of our member
(BLUEPRINT_ERROR) does not match the general meaning (usage
error). A future contributor might reasonably want to rename it;
the answer is documented in the exit-code reference doc.

### Follow-up actions

- [x] Introduce `ExitCode` enum in `forge.core.exceptions`
(`src/forge/core/exceptions.py`).
- [x] Map every existing `ForgeError` subclass to its `exit_code`
(see module docstring's "Hierarchy at a glance").
- [x] Route `handle_exception` through the enum — no bare `int`
literals in `src/forge/cli/errors.py`.
- [x] Make `forge validate` return `ExitCode` members exclusively
(`src/forge/cli/commands/validate.py`); no bare `1`, `2`, or `5`.
- [x] Add `docs/reference/exit-codes.md` with the full table and
the "streams are paired with codes" section.
- [ ] Extend `docs/reference/exit-codes.md` with a "consumer
recipes" section once `forge create` and `forge verify` also
adopt the enum (they have partial adoption today; full
adoption is tracked separately).
- [ ] Add a CI grep guard that fails a PR if
`src/forge/cli/commands/*.py` contains `return [0-9]` outside
of `int(ExitCode.X)` constructions. (Deferred: the grep needs a
precedent for what "acceptable bare integer" looks like once
`create` and `verify` are migrated; rushing the guard now would
produce false positives on the legacy paths.)
- [ ] Bump the CLI's minor version when the enum's semantics are
considered stable across all commands, and note the stability
in the changelog.

## Alternatives Considered

### Alternative 1: Single non-zero exit code (0 for success, 1 for all failures)

The simplest possible contract: `0` means success, `1` means
something failed. Every consumer branches on `if [ $? -ne 0 ]` and
reads stderr for details.

*Rejected* because it collapses the four outcomes above into one,
and every consumer that cares about the difference (which is every
non-trivial CI integration) reconstructs the distinction by parsing
stderr prose. That is the exact anti-pattern the JSON envelope in
ADR-0012 exists to avoid, reintroduced at the level of the process
status. It also makes "the tool crashed" indistinguishable from
"your blueprint is invalid" — a class of bug we would like to make
impossible, not merely rare.

Honest merit: it is the cheapest possible exit-code contract to
document, and for a CLI whose only consumer is a human at a
terminal, it is sufficient. Forge CLI's primary consumers are CI
and tooling, so the simplification is not free — it is deferred
onto every downstream script.

### Alternative 2: Per-command exit-code namespaces (e.g. 10–19 for `validate` errors, 20–29 for `create`)

Each command gets a numeric range, and a command's internal outcome
maps to a member of its range. A consumer that only cares about
`forge validate` sees `10 = invalid blueprint`, `11 = bad path`,
`12 = loader crash`. No two commands' codes collide.

*Rejected* because it re-invents the "distinguish the four outcomes"
problem in a harder-to-remember way (memorising which range belongs
to which command) while providing a benefit only if two commands
have semantically incompatible outcomes. In practice the six
cross-command outcomes (`success`, `user error`, `blueprint error`,
`filesystem conflict`, `verification failed`, `internal error`) are
shared across every command, so the ranges would repeat. The
benefit — namespace isolation — is real only in a codebase where
commands disagree about what "error" means, which is precisely the
disease this ADR cures.

Honest merit: numeric ranges are a well-established pattern (HTTP
status codes use them, and it is one of the reasons HTTP is so
legible). If Forge CLI ever grows a plugin model where third-party
commands have their own exit-code vocabularies that would clash
with ours, ranges become necessary. Today, no such model exists;
adding ranges now is preemptive complexity.

### Alternative 3: Symbolic-only exit codes (return string codes, translate to integers at the boundary)

A command returns `"user_error"` or `"internal_error"`, and
`main()` translates the string to a number based on a lookup table.
The numbers become an implementation detail of `main()`.

*Rejected* because shell scripts read integers, and there is no way
to translate a string to a number that is both stable across the
CLI's lifetime and free of lookup-table ambiguity ("was it
`user_error` or `user-error`?"). Strings also force `main()` to
be the sole arbiter of the number-to-name mapping, which means the
mapping lives in one function rather than in the enum — the same
one-place-of-truth property the enum provides, but without the
type checking that `ExitCode.USER_ERROR` gives a caller in the
command body.

Honest merit: symbolic codes decouple the CLI from the numeric
contract, which is genuinely useful if the numeric contract has
historically been unstable. It has not; the values are pinned by
this ADR for the foreseeable future. The decoupling is a
comfort, not a benefit.

### Alternative 4: Adopt a third-party exit-code standard (e.g. `sysexits.h`)

The BSD `sysexits.h` header defines well-known exit codes:
`EX_USAGE = 64`, `EX_DATAERR = 65`, `EX_SOFTWARE = 70`, and so on.
Some CLIs adopt them wholesale for the credibility of "standard"
codes.

*Rejected* because the standard's vocabulary does not match our
needs. `sysexits.h` distinguishes "input data format error" from
"input data value error" (`EX_DATAERR` covers both for us) and does
not have a good fit for "the blueprint was malformed but the tool
worked." Mapping our outcomes onto `sysexits.h` values would either
require four of them to collapse into `EX_DATAERR` (losing the
distinction we most need) or require adopting more of the standard
than we actually use. Adoption would also trade a small, memorable
six-value vocabulary for a large, standard-but-arbitrary one.

Honest merit: using `sysexits.h` aligns us with a widely-recognised
convention, and a consumer that already knows the standard has zero
learning curve. That is a real benefit for tools whose primary
audience is `cron` jobs and shell-script authors. Forge CLI's
primary consumers are CI scripts and IDE integrations that will be
written against our exit codes regardless; there is no existing
ecosystem to align with.

## References

- `docs/reference/exit-codes.md` — the full exit-code table.
- `docs/reference/CLI_VALIDATE_OUTPUT.md` — the JSON envelope
contract for `forge validate`, including the exit-code matrix
consumers branch on.
- ADR-0012 — *CLI Output Is a Versioned DTO Consumed by Pluggable
Renderers*. The exit code is the first field of the same
contract this ADR formalises.
- ADR-0005 — *Stabilize Exit Codes as a Public Contract* (the
precursor to this ADR; the current document supersedes and
extends it with the command-level mapping and the
`ExitCode` enum rationale).
- `sysexits.h` — BSD `sysexits.h` (rejected in Alternative 4).
- `src/forge/core/exceptions.py` — the `ExitCode` enum and the
`ForgeError` hierarchy that declares each subclass's
`exit_code`.
- `src/forge/cli/errors.py` — the single CLI boundary
(`handle_exception`) that maps an exception to an `ExitCode` and
to a rendered message.
- `src/forge/cli/commands/validate.py` — reference implementation
of a command that returns `ExitCode` members exclusively.
- `tests/unit/cli/test_validate_exit_codes.py` — the contract test
for `forge validate`'s exit codes.
- `tests/contract/test_validate_output_contract.py` — the
snapshot test for the JSON envelope that the exit codes
correspond to.
