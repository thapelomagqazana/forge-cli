# 0009. Blueprint Discovery Uses Layered Sources with a Central Resolver

- **Status:** Accepted
- **Date:** 2026-11-08
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

Forge ships with blueprints — declarative templates for common
project shapes (a FastAPI service, an automation CLI, a React
frontend). To be useful out of the box, the tool must find those
shipped blueprints without requiring any configuration from the user.

At the same time, users need to add their own blueprints without
forking the package, and they need to *override* a shipped blueprint
when their team's conventions diverge from the default (a different
license header, an additional CI step, a company-specific test
harness). The override must be per-project, not per-machine: a
developer working on two repositories in two terminals expects two
different catalogues, without any environment-variable fiddling.

Three constraints follow from those requirements.

**First, the bundled blueprints must actually ship.** A tool that
reads its defaults from the source tree works in development and
silently breaks for users who `pip install` it. The packaging
mechanism must survive editable installs, regular wheels, and (for
compatibility with some deployment pipelines) zipimported wheels.

**Second, local overrides must win deterministically.** "Sometimes my
override works, sometimes it doesn't" is not a bug report a tool can
recover from; it destroys user trust in the mechanism entirely. When
both layers offer the same blueprint id, the resolution must be
predictable from the file system layout alone.

**Third, one bad file must not take down the catalogue.** A user
with three local blueprints and one that fails to parse still needs
to see the other two and the full official set — the alternative
(a hard failure) leaves them unable to run `forge list` at all while
they fix the broken file, which is exactly when they need it most.

Finally, the resolution must be *extensible*. A future source — a
remote registry, an XDG cache, a workspace-vendored blueprint set —
should be addable without changing the precedence rules, the failure
policy, or the shape of the returned catalogue. If the rules for
"who wins" are scattered across sources, adding a source reopens
every precedence question.

## Decision

We will discover blueprints from **ordered, pluggable sources**
consumed by a **single central resolver** that owns precedence,
deduplication, warning emission, and ordering.

Concretely:

### Sources propose candidates; they do not decide anything

A source is any object exposing one method:

```python
def candidates(self) -> Iterator[tuple[str, bytes, BlueprintSource]]:
    ...
```

Each item is `(label, raw_bytes, origin)`. The label is a
human-readable provenance string (`"bundled:python.fastapi.api"`,
`"local:/abs/.forge/blueprints/x/blueprint.yaml"`); the bytes are the
unparsed manifest; the origin is the layer tag used for precedence.

Sources perform **only** I/O. They do not parse, validate, deduplicate,
sort, or resolve precedence. Every failure — a missing directory, an
unreadable file, a package that failed to import — is yielded as an
empty-bytes candidate with the appropriate label, so the resolver has
one failure mode to handle rather than four.

We will ship two sources in v1:

- **`BundledSource`** — reads from `forge.blueprints` via
  `importlib.resources.files()`. The choice of `importlib.resources`
  over `Path(__file__)` is load-bearing: it is the only API that
  resolves identically across editable installs, wheels, and
  zipimported wheels.
- **`LocalDirectorySource`** — reads from `.forge/blueprints/` in the
  **current working directory**. Not the home directory. Blueprints
  are project-shaped artefacts, so per-project overrides are the
  natural unit of customisation; a developer with two terminals in
  two projects gets two catalogues without configuration.

### A single resolver owns every policy decision

`forge.discovery.resolver.resolve(candidates)` consumes the source
stream and returns `ResolvedCandidate` objects. It is the *only*
place that:

- applies precedence (`_PRECEDENCE` is a module-level dict:
  `LOCAL > OFFICIAL`);
- deduplicates by blueprint id;
- emits warnings on duplicate collisions (naming both the winner and
  the loser so users can find the file to delete);
- emits warnings on malformed candidates (naming the file so users
  can fix it), and skips them without failing the catalogue;
- wraps every parse failure — empty bytes, invalid YAML, non-mapping
  root, schema violation, model violation — into a single
  `MalformedBlueprintError` so the skip path is one branch.

The resolver does **not** sort. Sorting is the projection's job
(`resolver.to_metadata`), so future callers — a validator that wants
the natural resolution order, a collision reporter — can consume
`resolve` without paying for a sort they do not need.

### The service is a thin orchestrator

`BlueprintDiscoveryService.discover()` composes the source list,
streams candidates through the resolver, and projects to
`DiscoveredBlueprint`. It has no policy and no cache:

- **No policy** — every decision lives in the resolver. A shortcut in
  the service ("skip the resolver when there's only one candidate")
  creates a second copy of the rules, which drifts.
- **No cache** — users edit blueprints mid-session; a cache means
  stale data with no invalidation story. Discovery is cheap on
  realistic catalogue sizes (tens of small YAML files). Callers that
  need caching layer it on top with their own invalidation logic.

### Precedence, failure policy, and ordering, stated once

- **Precedence:** local overrides official. Duplicates emit **one**
  warning naming both sides; the loser is dropped.
- **Failure policy:** one bad file never takes down the catalogue.
  Missing directories and empty directories are not failures — they
  are the default state of every fresh install and produce no
  warning.
- **Ordering:** the catalogue is sorted by id ascending. Determinism
  is a feature: `forge list` output is diffable in CI and
  snapshot-testable.

## Consequences

### Positive

- **The packaged tool works.** Using `importlib.resources` means the
  wheel actually contains what discovery reads. The
  `tests/integration/test_discovery_bundled.py` test proves this
  against a built wheel in a clean venv — the only test that can
  catch the "works in editable, ships empty" bug.
- **One place to change precedence.** Adding a source, or changing
  who wins when two sources collide, is a one-line edit to
  `_PRECEDENCE` plus the corresponding test and doc updates. Without
  the central resolver, every source would reimplement the rule and
  every change would require auditing all of them.
- **Failures degrade gracefully.** A user with a broken local
  blueprint still gets the full official catalogue plus a warning
  naming the file. This is the difference between "fix your file" and
  "you cannot use the tool until you fix your file."
- **Sources are trivial to test.** A fake source is one class with
  one method; a fake resolver is unnecessary because the real one is
  small. The test suite exercises the composition path against fakes
  and the real filesystem, without mocking the policy.
- **The catalogue shape is stable.** `DiscoveredBlueprint` carries
  only what a listing needs (id, name, version, description,
  category, source) — so `forge list --json` output does not ripple
  when the full document model changes.

### Negative

- **No caching means repeated YAML parses.** Every `discover()` call
  re-parses every candidate. On a realistic catalogue this is
  sub-100 ms, and the correctness benefit (user edits are picked up
  immediately) outweighs it — but a future daemon-facing use case
  will need a cache layer above the service, with its own
  invalidation story.
- **Precedence rules are invisible to authors of new sources.**
  A contributor writing a `RemoteRegistrySource` needs to know that
  `_PRECEDENCE` exists and that their origin needs an entry there.
  The alternative — encoding precedence in each source — makes the
  rule visible to authors but impossible to reason about globally.
  This is the correct trade-off, but it needs a documented
  extension path (see ADR-0009's companion
  `docs/architecture/discovery.md`).
- **The bundled source depends on packaging.** If
  `[tool.hatch.build.targets.wheel].artifacts` in `pyproject.toml`
  ever drops the `blueprints/**` glob, discovery silently finds zero
  official blueprints in the wheel while working fine in development.
  The wheel-level integration test is the only protection against
  this, which is why it is not optional.
- **The enum `BlueprintSource` is closed.** Adding a new origin (say,
  a remote registry) requires editing the enum, the resolver's
  `_PRECEDENCE` dict, and the tests. This is correct — the set of
  origins is small and deliberate — but it does mean a new source is
  never *purely* additive; it always requires a small code change in
  the resolver.

### Follow-up actions

- [x] Implement `BundledSource` and `LocalDirectorySource` behind a
      shared `candidates()` contract (ADR-0009, `sources.py`).
- [x] Implement `resolve()` and `to_metadata()` with the precedence
      table, dedup logic, warning emission, and malformed-skip path
      (ADR-0009, `resolver.py`).
- [x] Implement `BlueprintDiscoveryService` as a policy-free
      orchestrator (`service.py`).
- [x] Add the wheel-level integration test proving bundled blueprints
      ship and are discoverable from an installed wheel
      (`tests/integration/test_discovery_bundled.py`).
- [x] Register `src/forge/blueprints/**` in
      `[tool.hatch.build.targets.wheel].artifacts` in
      `pyproject.toml` — this is the packaging change that makes the
      decision load-bearing rather than aspirational.
- [x] Publish `docs/architecture/discovery.md` as the narrative
      companion to this ADR.
- [ ] *When a `--blueprint-dir` CLI flag is added:* thread the flag
      through `default_sources(local_root)` — the seam exists but is
      not yet wired to a flag.
- [ ] *When a remote registry source is added:* add a
      `BlueprintSource.REMOTE` entry, extend `_PRECEDENCE` (likely
      below `LOCAL`, above or below `OFFICIAL` depending on the
      use case), and add a new source class. Precedence rules
      themselves do not change; only their table does.

## Alternatives Considered

### Alternative 1: Single hard-coded blueprint location (bundled only)

Read blueprints exclusively from `forge.blueprints` inside the
package. No user extension point.

**Rejected because** it forecloses the primary use case the local
layer exists for: teams with conventions that diverge from the
default. A user with a company-specific license header or an extra
CI step would have to fork the package and maintain the fork — a
maintenance burden that no amount of documentation reduces. The
cost of the local layer is one extra source (~40 lines) and one
extra precedence entry; the cost of *not* having it is that every
nonstandard team forks the tool.

### Alternative 2: Multiple sources with no central resolver

Each source decides for itself how to handle collisions with other
sources. A source that finds a local copy of an official blueprint
would either yield it (implicit "local wins") or skip it (implicit
"official wins").

**Rejected because** it makes precedence a property of each source
rather than a property of the system. Two sources that disagree about
who wins produce non-deterministic output depending on iteration
order — the "sometimes my override works" bug report this ADR exists
to prevent. Worse, adding a third source would require reasoning
about precedence *combinatorially* against the first two, and the
number of disagreements grows with the square of the source count.
A central resolver collapses that to a single lookup table.

The cost is that a new source author must know the resolver exists.
That is a documentation problem, solved by
`docs/architecture/discovery.md`'s "Extension points" section, not a
design problem.

### Alternative 3: Merge blueprint fields across sources

When two sources offer the same id, merge their fields — taking
variables from one, files from another, and so on.

**Rejected because** merging makes the resolved blueprint depend on
*which* files happen to be present, which is exactly the
non-determinism the design is built to avoid. It also invites a
whole class of "I added a variable locally and it appeared but my
file override didn't" bugs, because the merge rule for each field
would need to be specified and tested independently. A blueprint is
a document, not a partial overlay; the correct unit of override is
the whole document.

### Alternative 4: Cache discovery results in a module-level singleton

The service returns a cached catalogue; a `forge discover --refresh`
command invalidates the cache.

**Rejected because** it introduces cache invalidation as a user
concern. Users edit blueprints mid-session all the time — in a
terminal next to `forge create`, in an editor while `forge list` is
open. A cache means they see stale data with no way to know, and the
refresh command is a footgun for anyone who forgets it exists.

Discovery is cheap: tens of small YAML files, each parsed in
milliseconds. The correctness benefit of no-cache is worth more than
the performance cost, and callers with a genuine performance need
(a long-running daemon) can layer their own cache above the service
with their own invalidation story. That makes cache correctness
*their* concern and the service provably correct regardless of who
calls it.

### Alternative 5: Local directory at `~/.forge/blueprints/` (home directory)

Read local blueprints from the user's home directory, so a single
override applies to every project.

**Rejected because** blueprints are project-shaped artefacts. They
reference templates relative to themselves, generate files into a
project directory, and encode conventions that differ between
projects (a Python project and a TypeScript project will not share
blueprints). A per-machine override would surprise users who work on
multiple projects and expect the tool to respect the project
boundary — the same expectation `pre-commit`, `pyproject.toml`, and
`.env` files rely on.

The home-directory location can be added later as a *third* source
without changing this ADR's decision: the resolver already handles
arbitrary source lists, and adding an entry is a small, additive
change. Freezing the CWD as the default now preserves that
extensibility while giving users the behaviour they expect today.

## References

- Implementation:
  - `src/forge/discovery/sources.py` — the source contract and the
    two concrete sources.
  - `src/forge/discovery/resolver.py` — precedence, dedup, warnings,
    ordering.
  - `src/forge/discovery/service.py` — the orchestration façade.
  - `src/forge/discovery/errors.py` — `MalformedBlueprintError`.
- Companion documentation:
  - `docs/architecture/discovery.md` — the narrative companion to
    this ADR; the "how it works" to this document's "why it works
    this way."
- Related ADRs:
  - `docs/adr/0007-blueprint-schema-as-canonical-contract.md` —
    what a blueprint *is*. Discovery consumes valid documents; the
    schema is the referee for what "valid" means.
  - `docs/adr/0008-schema-generated-pydantic-models.md` — the typed
    projection that discovery returns and the parity guarantees that
    make `DiscoveredBlueprint` safe to consume.
- Test suite:
  - `tests/unit/discovery/test_sources.py` — source contract, I/O
    failure paths.
  - `tests/unit/discovery/test_resolver.py` — precedence, dedup,
    warnings, malformed skip.
  - `tests/unit/discovery/test_service.py` — orchestration.
  - `tests/unit/discovery/test_no_local.py` — the default state
    (no local directory) works.
  - `tests/unit/discovery/test_malformed_skip.py` — the failure
    policy (skip with warning, don't crash).
  - `tests/integration/test_discovery_bundled.py` — the
    *built wheel* actually ships the blueprints.
- Packaging:
  - `pyproject.toml` — `[tool.hatch.build.targets.wheel].artifacts`
    is where the bundled blueprints are declared as package data.
    Without the `blueprints/**` glob there, discovery finds zero
    official blueprints in the wheel while working correctly in
    development.
