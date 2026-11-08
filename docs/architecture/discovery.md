# Blueprint Discovery

How Forge finds, deduplicates, and orders the blueprints available on
a machine — and why the answer looks the way it does.

This document describes the *implementation* of the decision recorded
in [ADR-0009](../adr/0009-blueprint-discovery-and-precedence.md). If
the two disagree, the ADR wins and this file is a bug.

---

## What discovery answers

One question, one answer:

> *Which blueprints are available on this machine, in what order, and
> where did each one come from?*

Discovery returns a **catalogue**: an immutable, ID-sorted list of
`DiscoveredBlueprint` values. Each entry carries:

| Field | Meaning |
|-------|---------|
| `id` | Stable dotted identifier (e.g. `python.fastapi.api`) |
| `name` | Human-readable display name |
| `version` | SemVer string |
| `description` | One-paragraph explanation |
| `category` | Coarse classifier (`api`, `automation`, …) |
| `source` | `official` (bundled) or `local` (user-supplied) |

The catalogue is a *view*, not the full blueprint document. Listing
dozens of blueprints should not require parsing dozens of templates
and verification commands; that work belongs to `load()`, which
returns the full typed `Blueprint` for a single id on demand.

---

## The two layers

Forge looks for blueprints in exactly two places, in this order:

### 1. Bundled (official)

Shipped inside the wheel at `forge.blueprints.<id>/blueprint.yaml`,
reached via `importlib.resources.files("forge.blueprints")`.

These are the blueprints that make Forge useful out of the box. They
are versioned with the tool, reviewed like source code, and validated
against the canonical schema like any other blueprint (see
[ADR-0007](../adr/0007-blueprint-schema-as-canonical-contract.md)).

Using `importlib.resources` — not `Path(__file__)` — is non-negotiable.
It is the only API that behaves identically across editable installs,
regular wheels, and zip-imported wheels. A bundled source that used
`__file__` would work in development and silently break for end users,
which is the exact failure mode this design exists to prevent.

### 2. Local (user-supplied)

Read from `.forge/blueprints/` in the *current working directory* —
not the home directory. The reasoning (see ADR-0009, "Default
location") is that blueprints are project-shaped artefacts: they
reference templates relative to themselves, generate into a project
directory, and are naturally scoped to a repository. A developer
working on two projects in two terminals expects two different
catalogues, without any environment-variable fiddling.

Layout mirrors the bundled one, so a user can copy an official
blueprint into `.forge/blueprints/` and edit it in place without
renaming anything:

```
.forge/blueprints/
├── python.fastapi.api/          # directory name should match manifest id
│   ├── blueprint.yaml
│   └── templates/...
└── my.custom.bp/
    └── blueprint.yaml
```

---

## Precedence

When two layers offer a blueprint with the same `id`, **local wins**.

The rule exists so users can override official blueprints without
forking the package. It is a hard rule, not a "usually" — a local
copy *always* shadows the official one, and the resolution emits a
warning naming both files so the user knows which one is in effect:

```
WARNING  forge.discovery:local:/abs/.forge/blueprints/python.fastapi.api/blueprint.yaml
         overrides bundled:python.fastapi.api
```

The warning names the winner *and* the loser. A message that said only
"duplicate blueprint found" would leave the user searching their disk
for the file to delete; naming both sides makes the fix a single
`rm`.

### Why not "merge"?

There is no merge. A blueprint is a document, not a partial overlay;
merging fields across sources would make the resolved blueprint depend
on which files happened to be present, which is exactly the
non-determinism the design is built to avoid.

### Why not "warn but use official"?

Two reasons:

1. Users override official blueprints *on purpose*. Reversing the
   precedence would silently disable their customisations, which is
   worse than the alternative of making them re-read their own files.
2. The local copy is the user's most recent intent — they wrote it
   after reading the official one. The freshest intent should win.

---

## Failure policy

The design principle is: **one bad file never takes down the
catalogue.**

| Situation | Result |
|-----------|--------|
| No local directory | Official catalogue loads normally, no warning |
| Empty local directory | Official catalogue loads normally, no warning |
| Malformed local blueprint | Skipped with a warning naming the file |
| Unreadable local blueprint | Skipped with a warning naming the file |
| Malformed bundled blueprint | Skipped with a warning (packaging bug — should be caught in CI) |
| Duplicate ID | Higher-precedence source wins; one warning names both sides |

In every failure case, the rest of the catalogue survives. A
developer with three local blueprints and one broken one still sees
the other two plus the full official set — which is the only
behaviour that lets them make forward progress while they fix the
broken file.

**No local directory is not a failure.** Most users have no
`.forge/blueprints/` at all; that is the default state of every
fresh install. Warning about it would train users to ignore
warnings. The [no-local test](../../tests/unit/discovery/test_no_local.py)
exists specifically to pin this property.

---

## Architecture

Discovery is a small, layered subsystem. Each layer has one job, and
the dependency arrows all point inward:

```
                     ┌──────────────────────────┐
                     │  BlueprintDiscoveryService│   ← public façade
                     └────────────┬─────────────┘
                                  │
                     ┌────────────┴─────────────┐
                     │      resolver.py         │   ← precedence, dedup, warnings, order
                     └────────────┬─────────────┘
                                  │
                     ┌────────────┴─────────────┐
                     │       sources.py         │   ← candidate streams (I/O only)
                     └────────────┬─────────────┘
                                  │
                     ┌────────────┴─────────────┐
                     │   verification (funnel)  │   ← schema + model validation
                     └──────────────────────────┘
```

### `sources.py` — where to look

A source's only job is to yield `(label, raw_bytes, origin)` tuples.
It does not parse, does not validate, does not decide precedence, does
not sort. Two sources ship today — `BundledSource` and
`LocalDirectorySource` — and both obey the same one-method contract:
`candidates()`.

Sources never raise to their caller. Every I/O failure — a missing
directory, an unreadable file, a package that failed to import —
yields an empty-bytes candidate the resolver handles uniformly. That
single error path is what lets the resolver stay one branch wide.

### `resolver.py` — who wins

The resolver consumes the candidate stream and produces
`ResolvedCandidate` objects. It owns exactly three things:

- **Precedence**, encoded as a module-level `_PRECEDENCE` dict.
  Changing the rule is a one-line edit; testing it is a matter of
  asserting on a table.
- **Deduplication by id**, with a warning on every collision.
- **Translation of every parse failure into `MalformedBlueprintError`**,
  so skipping malformed candidates is one branch, not four.

The resolver returns an *unordered* list. Sorting is the projection's
job, not the resolver's, so future callers (a validator, a collision
report) can inspect the natural order without paying for a sort they
do not need.

### `service.py` — orchestration

The service composes the source list, streams candidates through the
resolver, and projects to `DiscoveredBlueprint`. That is all it does.
It has **no policy**: no caching, no filtering, no re-sorting, no
duplicate checks. Every decision that could be called policy lives in
the resolver; every I/O decision lives in the sources.

The service exposes two methods:

- `discover() -> list[DiscoveredBlueprint]`
- `load(id) -> Blueprint`

Both re-resolve on every call. There is deliberately **no cache**:

1. Users edit blueprints mid-session. A cache means stale data with
   no way to invalidate it.
2. Discovery is cheap on realistic catalogue sizes (tens of small
   YAML files). A cache would be a correctness risk paying for a
   performance problem users do not have.

Callers that need caching — a long-running daemon, say — layer it on
top with their own invalidation story. That keeps cache correctness
*their* concern and the service provably correct regardless of who
calls it.

---

## Extension points

The design anticipates two kinds of extension. Both are additive.

### Adding a source

Implement a class with one method:

```python
class MySource:
    def candidates(self) -> Iterator[Candidate]:
        yield ("mylabel:<id>", b"...", BlueprintSource.LOCAL)
```

Then register it:

1. Add it to `default_sources(local_root)` in `sources.py`, **or**
2. Inject it via `BlueprintDiscoveryService(sources=[...])` in a
   custom caller.

If the new source has a *different precedence*, add an entry to
`resolver._PRECEDENCE`. That is the only place a precedence rule
lives; the source does not need to know it exists.

If the new source has a *different origin* (say, a remote registry),
add a value to `BlueprintSource` in `models/blueprint.py`. The enum is
`str`-valued, so it serialises into JSON logs and `forge list --json`
output without a custom encoder.

### Changing precedence

Edit one dictionary:

```python
_PRECEDENCE: dict[BlueprintSource, int] = {
    BlueprintSource.OFFICIAL: 10,
    BlueprintSource.LOCAL: 20,
}
```

Then update:

- The `TestPrecedenceTable` tests in
  `tests/unit/discovery/test_resolver.py` (they assert the numeric
  values and the "local beats official" consequence).
- ADR-0009 (the decision record).
- This document.

The list of files to update is the reason the rule is data. A rule
scattered across sources has no such list; every source would need to
be re-audited, and reviewers would have to notice an implicit change.

---

## What discovery is *not*

Three things that look like they belong in discovery but do not:

**Not the wizard.** The interactive `create` command asks the user
which blueprint to use, then calls `service.load(id)` to get the
variables it needs. The wizard's UI, prompts, and validation live in
`forge.cli.prompts` and `forge.cli.commands.create`; discovery only
provides the catalogue and the load method.

**Not scaffolding.** Generating files from a blueprint is the
generation layer's job. Discovery stops at "here is the typed
`Blueprint` object"; everything after that — file copying, template
rendering, verification — is downstream.

**Not verification.** Discovery validates that a blueprint *is* a
valid blueprint (via `validate_blueprint_document`, which runs the
schema and the model). It does not verify that a *generated project*
works — that is `forge verify`'s job, and it happens after
scaffolding, on the project directory, not on the blueprint.

The line between these is worth understanding, because it is where
most "I thought discovery did that" bugs start. Discovery is the
first step of a longer pipeline:

```
discover  →  load  →  prompt  →  scaffold  →  verify
   ↑
   This document is about here.
```

---

## Testing

The discovery subsystem has a small, focused test suite. Each file
answers one question:

| File | Question |
|------|----------|
| `tests/unit/discovery/test_sources.py` | Does each source yield what it should, and only what it should, on both the happy path and every I/O failure? |
| `tests/unit/discovery/test_resolver.py` | Does the resolver apply precedence, warn on collisions, and skip malformed candidates, all in one place? |
| `tests/unit/discovery/test_service.py` | Does the orchestrator compose sources and the resolver correctly, without adding policy? |
| `tests/unit/discovery/test_no_local.py` | Does discovery work with no local directory — the default state of every fresh install? |
| `tests/unit/discovery/test_malformed_skip.py` | Does a broken local blueprint get skipped with an actionable warning, without taking down the rest of the catalogue? |
| `tests/integration/test_discovery_bundled.py` | Does a *built wheel* actually ship the bundled blueprints and discover them from a clean environment? |

Each file's docstring names what it does *not* test, and points at
the file that does. That discipline is the only thing preventing the
suite from drifting into a set of tests that all break together when
one rule changes.

---

## See also

- [ADR-0007: Blueprint schema as canonical contract](../adr/0007-blueprint-schema-as-canonical-contract.md) — what a blueprint *is*.
- [ADR-0008: Generated Pydantic models](../adr/0008-schema-generated-pydantic-models.md) — the typed projection discovery returns.
- [ADR-0009: Discovery and precedence](../adr/0009-blueprint-discovery-and-precedence.md) — the decision this document describes.
- [Exit codes reference](../reference/exit-codes.md) — what `DiscoveryError` means to scripts.
