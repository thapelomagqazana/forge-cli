# 0012. CLI Output Is a Versioned DTO Consumed by Pluggable Renderers

- **Status:** Accepted
- **Date:** 2026-11-09
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

The Forge CLI has more than one audience and more than one output
format, and the pressure to serve both from one command is growing
with every new subcommand.

**Two audiences, two contracts.** Humans read tables; automation reads
JSON. A human piping `forge list` into `less` wants aligned columns
and readable spacing. A script piping `forge list --json` into `jq`
wants byte-identical, ANSI-free, sorted JSON whose shape is stable
across releases. Both audiences are first-class, and a single output
format cannot serve both without compromising the other.

**Two formats, one source of truth.** Without a shared representation,
each command that emits both a table and JSON must implement its own
projection logic — one for the table, one for JSON — and those two
implementations inevitably drift. A field gets added to the table but
not the JSON; a status enum gains a member in one but not the other;
an ordering changes in the table but not in JSON. Every such drift
is a consumer-visible bug, and the bug report is always the same:
"the two outputs disagree."

**Two failure modes, silent.** When formatting and data selection are
entangled (a command body that builds strings directly, or that
serialises whatever dict it happens to have), two failure modes become
invisible:

1. **Machine output is corrupted by human framing.** A stray `print()`
   in the middle of a JSON path, an ANSI escape from a formatter that
   does not check TTY, a warning written to stdout instead of stderr.
   All three break `jq`. None fail in a test that only checks the
   table.
2. **Human output loses data because the JSON did not need it.** A
   column dropped from the table because it is derivable from other
   fields in JSON; a truncation applied globally because the JSON
   format wanted a short form. Both silently remove information the
   human audience asked for.

**Empty states are where CLIs break.** The most commonly broken
conventions in CLI output — empty results as failures, human-facing
messages emitted in JSON mode, exit 1 because a list happened to be
empty — all live at the boundary between "the command answered the
question" and "the command failed". A single, tested convention for
these cases eliminates the class of bug.

**Versioned shapes are how automation survives upgrades.** A JSON
shape is an API. The `forge list --json` output is consumed by
scripts, CI jobs, and downstream tooling — none of which the Forge
team can update atomically with the CLI. A shape that changes without
a version identifier leaves every consumer to discover the break at
runtime. A shape that carries its version in the envelope lets those
consumers write `if schema_version == "1.0.0": ...` and pin their
expectations explicitly.

The current state of the codebase has three of these problems
partially addressed by convention (renderers are pure, JSON is
versioned, empty is success) but no single decision that binds them
together. Each new command re-discovers the conventions by reading
the previous command's code, and a contributor who does not know
them re-invents a slightly different shape. The shape of one output
does not guarantee the shape of the next.

## Decision

**We will represent every command's output as a versioned Pydantic
DTO, and every command will produce its human and machine formats by
passing that DTO to pure, format-specific renderers.**

Concretely:

1. **Every command with more than one output format defines a
   versioned DTO** in `forge/cli/schemas/` — a Pydantic model with an
   envelope carrying a `schema_version` string (SemVer) and a payload
   whose shape is documented in `docs/reference/`. The DTO is the
   *contract*; the shape it declares is what automation consumes.

2. **Sorting and filtering happen once, in the command, before the
   DTO is built.** Renderers receive an already-ordered, already-
   filtered list and preserve it verbatim. A renderer that re-sorts
   or re-filters is a bug, not a feature — it lets the two formats
   disagree.

3. **Rendering is a pure function from DTO to string**, living in
   `forge/cli/renderers/`. Renderers have no I/O, no clock, no
   environment lookups, and no imports from `forge.cli.ui`. Given the
   same DTO, they produce the same string on every machine, in every
   locale, at every time of day. That is what makes snapshot tests
   meaningful and diffable.

4. **Colour and stream selection are applied by `ui.emit`, never by
   the renderer.** `emit` is the *only* place in the CLI that writes
   to stdout for command output. It appends exactly one trailing
   newline (POSIX text convention) and handles broken-pipe conditions.
   ANSI escapes never enter a renderer's output, so JSON remains
   valid and snapshot fixtures remain diffable.

5. **Machine-readable output (`--json`) is a public contract**,
   documented in `docs/reference/`, versioned in the DTO's
   `schema_version`, and guarded by a schema-snapshot test
   (`tests/contract/`). Any change to the shape requires a version
   bump and a documentation update, enforced by a CI test that fails
   when the DTO's version and its assertions disagree.

6. **Empty output is success (exit 0), not failure.** An empty
   catalogue is a valid answer to the question the command asked. In
   table mode, an empty state produces a human-facing warning on
   stderr and no stdout payload. In JSON mode, an empty state
   produces the valid empty envelope on stdout — never a
   human-facing message, never an empty string.

7. **Exit codes are part of the contract.** They are documented in
   one place per command (`docs/reference/`) and asserted on at the
   process boundary. A usage error (bad flag value) exits 2; a
   discovery or validation error exits 1 with a message on stderr;
   an unexpected internal error exits 5. Successes — including empty
   results — exit 0.

8. **Machine-readable output must be diffable and hashable.** The
   JSON renderer uses `sort_keys=True`, fixed separators, and
   `ensure_ascii=False`. Two invocations with the same environment
   produce byte-identical output, so `diff` and `sha256sum` are
   usable debugging and caching tools.

These rules are enforced by structure, not by convention:

- Renderer purity is checked by a test that greps the renderer
  module for `print` calls and `forge.cli.ui` imports.
- Determinism is checked by a test that runs the CLI twice and
  compares bytes.
- Version/shape coupling is checked by a test that fails when the
  DTO's `schema_version` and its shape assertions disagree.
- The empty-state convention is checked by tests that assert on exit
  code, stdout, and stderr for both output modes.

## Consequences

### Positive

- **Adding a format is additive.** `--format=yaml`, `--format=csv`,
  `--format=markdown` each becomes one new file in
  `cli/renderers/` and one new branch in the command. No existing
  renderer changes, no consumer of the current formats is at risk.
  The cost of the *second* format is paid once; every subsequent
  format is nearly free.

- **Table and JSON cannot disagree.** Both consume the same DTO, so
  a field added to one is automatically present in the other. The
  ordering is decided once, before the DTO is built, so both formats
  see the same order. The class of "table says X, JSON says Y" bug
  becomes structurally impossible.

- **Machine output is verifiably clean.** ANSI escapes cannot enter a
  payload because the renderer that produces it never emits them, and
  the writer that emits the payload never adds them. Human framing
  cannot leak into a payload because the human and machine paths are
  different renderers producing different strings, not the same
  string with mode-conditional branches.

- **Snapshot tests are meaningful.** Because renderers are pure and
  deterministic, a fixture under `tests/fixtures/` can assert on the
  exact bytes a renderer produces. Any drift shows up as a reviewable
  diff. Without purity, the same fixtures would be flaky.

- **Empty states are tested once and inherited.** A new command that
  reuses the DTO-and-renderers pattern gets the empty-state
  convention for free — the tests for it already exist and already
  pass.

- **The contract is discoverable.** A consumer reading
  `docs/reference/CLI_LIST_OUTPUT.md` learns the JSON shape, the
  exit codes, the ordering, and the empty-state behaviour in one
  place. A contributor reading the same document learns the same
  rules and — crucially — that they are tests, not preferences.

- **Downgrade-safe.** Because the DTO carries `schema_version` in
  the envelope, a consumer can detect a version it does not
  understand and fail explicitly. That is strictly better than a
  silent parse error or a missing field.

### Negative

- **More ceremony per command.** A command that emits both a table
  and JSON now needs a DTO, a table renderer, a JSON renderer, and
  contract tests. That is more files than a command that prints
  strings inline. The cost is paid once per command, and every
  command after the first amortises it: the DTO shape, the
  renderer contract, and the empty-state conventions are shared.

- **Pydantic is a dependency in the output path.** The DTO is a
  Pydantic model, so a command that renders output imports Pydantic
  (which it already does, via blueprint models) and pays the small
  model-validation cost on the DTO construction. On realistic
  catalogue sizes this is negligible; on pathological ones it is
  bounded by catalogue size, not by output size.

- **Snapshot fixtures require discipline.** Every renderer change
  that alters the byte output must regenerate the fixtures, and the
  diff must be reviewed. That is by design (the diff is the
  contract), but it introduces a *review* step where a strings-
  inlined command would have none.

- **Two test files per command, not one.** Unit tests cover the
  renderer and command seam; contract tests cover the observable
  output shape. The split is deliberate (each catches what the other
  cannot), but it doubles the test count for a given command.

- **Every shape change is a version bump.** Changing a field name
  requires bumping `schema_version` and updating the reference
  document. That is the correct cost for a consumer-facing
  contract, but it means small shape changes are not free.

- **Not every command needs this.** A command like `forge --version`
  has no meaningful JSON mode and does not benefit from a DTO. The
  rule applies to commands with machine-readable output; the
  *pattern* is available to any command that needs more than one
  format.

### Follow-up actions

- [ ] Add `--format` support to `forge list` as a future minor
      release, using the renderer package already in place. Confirm
      the existing table and JSON fixtures continue to pass.
- [ ] Add the same DTO-and-renderer pattern to `forge validate`
      (JSON error payloads). Introduce a `ValidationOutput` DTO with
      its own `schema_version` and reference document; reuse the
      renderer contract.
- [ ] Add the same pattern to `forge create` (JSON success
      payloads). Introduce a `CreateOutput` DTO and a renderer; the
      scaffold-side data is already a Pydantic model.
- [ ] Add a CI check that scans `docs/reference/CLI_*_OUTPUT.md` for
      a `schema_version` value and compares it against the
      corresponding DTO's `schema_version` field, failing when they
      disagree. The current guard test verifies *within* the file;
      this check verifies *across* files.
- [ ] Extract the "empty is success" rule from the per-command tests
      into a shared conftest fixture or test helper, so a new command
      that adopts the pattern can include the empty-state tests in
      one line.
- [ ] Document the renderer contract in a `docs/architecture/`
      page, so a contributor who is about to add a renderer reads
      it *before* writing tests that encode the wrong assumptions
      (e.g. that renderers may print, or that they may sort).
- [ ] Audit `forge list` and `forge validate` for any remaining
      `print()` calls outside `ui.emit`. The renderer-purity test
      catches this in renderers, but the command seam itself should
      also be checked (a command that prints directly is a command
      whose output a test cannot capture).

## Alternatives Considered

### Alternative 1: Print inline strings from each command

Each command builds its own output strings: the table, the JSON, the
error messages. No shared DTO; no renderer abstraction; no contract
document.

**Why rejected:** This is where the project started, and it is where
every one of the four failure modes above reappeared as the command
set grew. The friction of the first command's implementation is
low, but the *second* command pays the cost of re-deriving every
convention — which shape the JSON should have, which exit codes are
used, whether empty results are success, whether colour is applied
before or after serialisation. Those re-derivations are almost never
identical to the first command's, so consumers face a slightly
different shape per command. The cost of consolidation grows
quadratically with the number of commands; the cost of the shared
DTO pattern grows linearly.

### Alternative 2: Single format with a `--pretty` flag

One output shape (say, JSON) and a `--pretty` flag that adds
formatting. The table becomes "pretty JSON."

**Why rejected:** This conflates two concerns — the *shape* of the
data and the *presentation* of the shape — and forces the JSON
consumer to tolerate formatting it does not want (colour, aligned
columns, truncated values). The `--pretty` flag would need to guard
every formatting decision ("if pretty: pad; else: serialise"), which
is exactly the entanglement this ADR exists to eliminate. It also
cannot express the difference between "JSON is truncated for
readability" (a table concern) and "JSON is authoritative" (a
contract concern) — the consumer would need to know the truncation
policy to parse correctly, which defeats the point of the shape.

### Alternative 3: Version the *whole CLI*, not each output

Ship `forge list --json` with no envelope; consumers pin on
`forge --version`.

**Why rejected:** CLI version and output shape are different
concerns and change on different schedules. A patch release that
fixes a bug in `forge create` does not change `forge list --json`'s
shape; a minor release that adds a subcommand does not change any
existing output's shape. Coupling them means every patch release
is a compatibility scare for every consumer, and every shape change
is a version bump for unrelated features. A per-shape
`schema_version` in the envelope lets consumers ignore patch
releases and detect shape changes directly, which is strictly less
noise.

### Alternative 4: JSON Schema for the output, validated at runtime

Declare the output shape as a JSON Schema (like the blueprint
schema), validate it at render time, and use JSON Schema tooling
for version management.

**Why rejected:** JSON Schema is the right tool for the *blueprint*
shape because blueprints are user-authored and cross-language
validation is required. The *output* is generated by the CLI itself,
in Pydantic, from a Pydantic model — a JSON Schema in the middle
would be a second source of truth for the same shape, which is the
exact drift problem ADR-0007/0008 solved for blueprints. Instead,
the DTO is Pydantic, the shape is asserted in tests, and the
reference document is generated by hand from the same model. If the
output needs to be consumed by a non-Python tool that wants a JSON
Schema, one can be generated from the Pydantic model as a
publishing step — but it is derived, not authoritative.

### Alternative 5: Print JSON on stdout, human messages on stderr

No DTO; JSON is just a `json.dumps` of whatever the command happens
to have; the table is the same data pretty-printed.

**Why rejected:** This is Alternative 2 with a different name. It
solves the stream-separation problem (which is necessary but not
sufficient) while leaving the shape problem intact. `json.dumps` of
"whatever the command happens to have" is exactly the drift
described in the Context section: two commands emit two shapes,
consumers need to know which command they are parsing, and no
mechanism catches the divergence before release.

### Alternative 6: `click.testing.CliRunner` for all output tests

Skip subprocess tests; use `CliRunner` exclusively. Assert on
`result.output`, `result.exit_code`.

**Why rejected:** `CliRunner` runs in-process and bypasses the entry
point, the console-script wrapper, and any environment setup that
happens at process start. That is a *good* property for behaviour
tests (fast, focused) and a *bad* property for contract tests (the
contract is observable at the process boundary, and only a
subprocess observes it). The pattern adopted by this ADR is: unit
and contract tests both exist, at different layers, for different
purposes. `CliRunner` is not used because it would silently couple
the contract to the in-process behaviour, which is a strict subset
of the real behaviour.

## References

- [`docs/reference/CLI_LIST_OUTPUT.md`](../reference/CLI_LIST_OUTPUT.md)
  — the reference document for `forge list`'s output contract, which
  embodies the shape of the pattern.
- [`src/forge/cli/renderers/`](../../src/forge/cli/renderers/) — the
  renderer package, whose module docstring states the six rules a
  renderer must follow.
- [`src/forge/cli/schemas/`](../../src/forge/cli/schemas/) — the DTO
  package, whose façade states the versioning and re-export
  conventions.
- [ADR-0004](./0004-centralize-cli-output-and-error-handling.md) —
  centralised output and error handling, the pre-requisite for
  `ui.emit` being the only writer.
- [ADR-0007](./0007-blueprint-schema-as-canonical-contract.md) —
  the analogous decision for the *blueprint* schema, whose shape
  this ADR's output-shape pattern was modelled on.
- [ADR-0008](./0008-schema-generated-pydantic-models.md) —
  the analogous decision for *blueprint* models, whose
  generated-and-parity-tested approach this ADR's output-shape
  pattern deliberately does *not* copy (output shape is defined by
  hand, in Pydantic, because it is not authored by users).
- [ADR-0009](./0009-blueprint-discovery-and-precedence.md) — the
  analogous decision for *discovery*, whose empty-versus-broken
  distinction is the discovery-side half of the empty-is-success
  rule.
- [Twelve-Factor App, factor VI](https://12factor.net/processes)
  — "execute the app as one or more stateless processes" — the
  determinism rule is a corollary of this factor for CLI output.
- [CLI Guidelines](https://clig.dev/) — the general principles this
  ADR's empty-is-success and stdout-versus-stderr rules align with.
