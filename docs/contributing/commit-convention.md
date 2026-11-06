# Commit Convention

Forge CLI follows the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
specification. Every commit message on `main` follows a predictable structure,
which enables automated changelog generation, semantic versioning, and
searchable history.

This page is the canonical reference. If anything in the repository disagrees
with this page, this page wins — and the disagreement should be reported as a
bug.

---

## Table of Contents

- [Why a Convention?](#why-a-convention)
- [Format](#format)
- [Types](#types)
- [Scopes](#scopes)
- [Subject Rules](#subject-rules)
- [Body Rules](#body-rules)
- [Footer Rules](#footer-rules)
- [Breaking Changes](#breaking-changes)
- [Examples](#examples)
- [Enforcement](#enforcement)
- [Frequently Asked Questions](#frequently-asked-questions)
- [Remembering the Convention](#remembering-the-convention)

---

## Why a Convention?

A commit message is a message to two audiences:

1. **Future you** — six months from now, when you are debugging a regression
   and need to know *why* a line of code exists.
2. **Automated tools** — changelog generators, semantic versioning tools, and
   `git log --grep` all depend on a predictable commit structure.

Without a convention, `main` becomes a wall of "fix stuff", "update", "wip",
and "asdf". With a convention, `main` becomes a **narrative** — every commit
tells you what kind of change it is before you read a single word of body text.

### What the convention buys us

| Benefit | How the convention enables it |
| :--- | :--- |
| **Automatic changelog** | The changelog is grouped by type: Features, Bug Fixes, etc. |
| **Semantic versioning** | `feat:` → minor bump. `fix:` → patch bump. `feat!:` → major bump. |
| **Searchable history** | `git log --grep="^feat"` lists every feature ever added. |
| **Reviewer context** | A reviewer sees the type in the PR title and knows what to look for. |
| **Bisect precision** | When bisecting a regression, `fix:` commits are usually the culprit. |
| **Release notes** | Release notes are assembled from commit messages, not hand-written. |

---

## Format

Every commit message follows this structure:

```
<type>(<scope>): <subject>

<body>

<footer>
```

- **`<type>`** — required. One of the [types](#types) below.
- **`(<scope>)`** — optional but recommended. The area of the codebase affected.
- **`<subject>`** — required. A short imperative description of the change.
- **`<body>`** — optional. Explains *why* the change was made.
- **`<footer>`** — optional. References issues, breaking changes, co-authors.

A commit message that only has a subject is valid. A commit message that has a
body but no subject is not.

---

## Types

Every commit must use exactly one of the following types. The type is the first
word in the subject line, before any scope.

| Type | When to use | Version impact |
| :--- | :--- | :--- |
| `feat` | A new feature for the user. | **Minor** bump |
| `fix` | A bug fix for the user. | **Patch** bump |
| `docs` | Documentation only — no code change. | None |
| `test` | Adding or fixing tests — no production code change. | None |
| `refactor` | A code change that neither fixes a bug nor adds a feature. | None |
| `perf` | A performance improvement. | **Patch** bump |
| `build` | Changes to the build system or external dependencies. | **Patch** bump |
| `ci` | Changes to CI configuration or workflows. | None |
| `chore` | Other changes that don't modify `src/` or `tests/`. | None |
| `style` | Formatting, whitespace, semicolons — no logic change. | None |
| `revert` | Reverting a previous commit. | Depends on the revert |

### Type decision tree

Use this decision tree to pick the right type:

```
Is the change a new capability the user can see?
├── Yes → feat
└── No
    ├── Does it fix a bug the user can see?
    │   ├── Yes → fix
    │   └── No
    │       ├── Does it only change documentation?
    │       │   ├── Yes → docs
    │       │   └── No
    │       │       ├── Does it only change tests?
    │       │       │   ├── Yes → test
    │       │       │   └── No
    │       │       │       ├── Does it change only formatting or whitespace?
    │       │       │       │   ├── Yes → style
    │       │       │       │   └── No
    │       │       │       │       ├── Does it only change CI or build config?
    │       │       │       │       │   ├── Yes → ci or build
    │       │       │       │       │   └── No
    │       │       │       │       │       ├── Is it a code change with no behavior change?
    │       │       │       │       │       │   ├── Yes → refactor
    │       │       │       │       │       │   └── No → perf (if it improves speed)
```

When two types could apply, **prefer the type that describes the user-visible
effect**, not the implementation detail. A refactor that also fixes a bug is a
`fix`, not a `refactor`.

---

## Scopes

Scopes are optional but recommended. They narrow the type to a specific area of
the codebase, making the history easier to filter. The scope appears in
parentheses immediately after the type.

### Canonical scopes

Use one of these scopes when applicable:

| Scope | Covers |
| :--- | :--- |
| `cli` | `src/forge/cli/` — CLI commands, entry point, argument parsing |
| `core` | `src/forge/core/` — shared domain logic, config, exceptions, models |
| `blueprints` | `src/forge/blueprints/` — blueprint definitions |
| `schemas` | `src/forge/schemas/` — JSON schemas (blueprint, manifest, output) |
| `generation` | `src/forge/generation/` — template rendering, file writing |
| `verification` | `src/forge/verification/` — verification engine and check handlers |
| `manifests` | `src/forge/manifests/` — project manifest handling |
| `diagnostics` | `src/forge/diagnostics/` — `forge doctor` |
| `output` | `src/forge/output/` — JSON and human-readable output |
| `deps` | `pyproject.toml`, `uv.lock` — dependency updates |
| `ci` | `.github/workflows/` — CI configuration |
| `docs` | `docs/` and root markdown files |

### When to omit the scope

Omit the scope when the change spans multiple areas and no single scope is
dominant. Example: a change that touches `src/forge/cli/` and
`src/forge/core/` equally is a `feat:` with no scope.

Do **not** invent new scopes without updating this document. An unrecognized
scope is a documentation gap, not a creative writing opportunity.

---

## Subject Rules

The subject is the first line of the commit, after the type and optional scope.

| Rule | Example |
| :--- | :--- |
| Imperative mood | `add list command`, not `adds` or `added` |
| Lowercase first letter | `add list command`, not `Add list command` |
| No trailing period | `add list command`, not `add list command.` |
| Maximum 72 characters | Longer subjects wrap in terminals and truncate in GitHub |
| Describe *what*, not *why* | The body explains *why* |

### Why imperative mood?

Imperative mood matches the convention used by `git` itself. When you run
`git merge`, Git writes "Merge branch 'foo'". When you revert, Git writes
"Revert '...'". Imperative mood reads as *"if applied, this commit will..."* —
`add list command` reads as *"if applied, this commit will add the list command"*.

### Why lowercase?

Lowercase is the default output of `git log --oneline`, and it avoids
inconsistency (some commits capitalized, some not). It also keeps the subject
visually distinct from the capitalized type prefix.

### Why no trailing period?

Trailing periods are visual noise in `git log --oneline`. The subject is a
title, not a sentence.

---

## Body Rules

The body is optional but **strongly recommended** for anything beyond a trivial
change.

| Rule | Why |
| :--- | :--- |
| Blank line between subject and body | Git requires it; without it, tools misparse the message |
| Wrap at 72 characters | Terminals and `git log` truncate at ~72 |
| Explain *why*, not *what* | The diff already shows the *what* |
| Separate paragraphs with blank lines | Improves readability |
| Use bullet points for enumerations | Improves scannability |
| Reference the motivation, not just the change | "Because X was broken" > "Change Y" |

### Example body

```
fix(blueprint): reject empty project names during validation

The pattern `^[a-z][a-z0-9-]*$` allowed empty strings when the first
character was optional. The pattern now requires at least one character.

This bug was reported by users who passed `--name ""` and got an empty
project directory with no error message.

Fixes #57
```

Notice the body explains the *why* (a bug reported by users), not the *what*
(which the diff already shows).

---

## Footer Rules

Footers are optional. They appear at the end of the message, after a blank line.

### Issue references

| Footer | Effect |
| :--- | :--- |
| `Closes #123` | Auto-closes the issue on merge |
| `Fixes #456` | Same as `Closes` |
| `Resolves #789` | Same as `Closes` |
| `Refs #101` | Links the issue without closing it |
| `Part of #102` | Links the issue as part of a larger epic |
| `Depends on #103` | Documents a dependency |

### Breaking change reference

```
BREAKING CHANGE: <description>
```

See [Breaking Changes](#breaking-changes) below.

### Co-authors

```
Co-authored-by: Name <email@example.com>
```

Used when a commit has multiple authors. Common when pairing or when a reviewer
contributes significantly.

---

## Breaking Changes

A breaking change is any change that requires users to update their code,
config, or workflow. A breaking change triggers a **major** version bump.

### How to mark a breaking change

There are two ways. Use **both** for maximum clarity:

**1. Add `!` after the type/scope:**

```
feat(cli)!: rename `forge new` to `forge create`
```

**2. Include a `BREAKING CHANGE:` footer:**

```
feat(cli)!: rename `forge new` to `forge create`

The `forge new` command has been renamed to `forge create` for consistency
with the rest of the CLI.

BREAKING CHANGE: The `forge new` command has been renamed to `forge create`.
Scripts that call `forge new` directly must be updated to call `forge create`.
A deprecation shim will be added in v0.2.0 and removed in v1.0.0.

Migration guide: docs/migration/forge-new-to-forge-create.md
```

### What counts as breaking

| Change | Breaking? |
| :--- | :--- |
| Removing a CLI command | **Yes** |
| Renaming a CLI command | **Yes** |
| Removing a CLI flag | **Yes** |
| Changing a CLI flag's meaning | **Yes** |
| Removing a public Python API | **Yes** |
| Changing a public Python API signature | **Yes** |
| Changing the blueprint schema in a way that invalidates existing blueprints | **Yes** |
| Changing the manifest schema in a way that invalidates existing manifests | **Yes** |
| Changing the JSON output schema in a way that invalidates existing parsers | **Yes** |
| Adding a new CLI command | No |
| Adding a new CLI flag | No |
| Adding a new blueprint | No |
| Adding a new check type | No |
| Fixing a bug | No (unless the fix changes documented behavior) |

When in doubt, **treat it as breaking**. A major bump is cheap; a broken user
workflow is expensive.

---

## Examples

### Feature with scope

```
feat(cli): add `forge list` command

The `list` command discovers all available blueprints from both the bundled
package and the user's local `.forge/blueprints/` directory, then displays
them in a Rich table sorted by ID.

Closes #42
```

### Feature without scope (spans multiple areas)

```
feat: add project manifest generation

The manifest is written to `.forge/manifest.json` at project creation time.
It records the blueprint ID, blueprint version, Forge version, resolved
configuration, and the expected baseline checks.

This is the foundation for future lifecycle management (drift detection,
safe updates).

Closes #61
Part of #60
```

### Bug fix with regression test reference

```
fix(blueprint): reject empty project names during validation

The pattern `^[a-z][a-z0-9-]*$` allowed empty strings when the first
character was optional. The pattern now requires at least one character.

A regression test was added at
`tests/unit/schema/test_validation.py::test_empty_project_name_rejected`.

Fixes #57
```

### Documentation only

```
docs(blueprints): document the variable schema

Added a reference table for all supported variable types (string, boolean,
enum) with examples of each. Also documented the `validation` field for
pattern and enum constraints.

Refs #88
```

### Test only

```
test(verification): add timeout handling test for command runner

The command runner previously had no test for the timeout path. This test
spawns a subprocess that sleeps for 10 seconds with a 1-second timeout,
then asserts that the subprocess is terminated and a timeout diagnostic is
returned.

Refs #73
```

### Refactor

```
refactor(core): extract exit code mapping into a dedicated module

Moved the exit code enumeration and mapping from `cli/app.py` to
`core/exits.py`. This allows the mapping to be reused by future
subcommands and tested in isolation.

No behavior change.
```

### Performance

```
perf(generation): cache rendered Jinja2 templates

Templates are now compiled once and cached by path. Rendering the
Python API blueprint went from 340ms to 180ms in the benchmark suite.

Refs #94
```

### Build

```
build(deps): bump ruff from 0.4.4 to 0.4.5

Ruff 0.4.5 includes a fix for a false positive in the `RUF` rule set
that was affecting our CI. No code changes were required.

Refs #101
```

### CI

```
ci: add macOS to the test matrix

The macOS runner catches case-sensitivity and path-handling bugs that
the Linux runner misses. This commit adds `macos-latest` to the
`quality` job matrix.

Refs #99
```

### Revert

```
revert: feat(cli): add `forge list` command

This reverts commit 1a2b3c4d.

The `list` command broke `forge doctor` because both commands registered
a `list` subcommand in Typer. Reverting until the registration conflict
is resolved.

Refs #63
```

### Breaking change

```
feat(manifest)!: change manifest schema to v2

The manifest schema is now versioned separately from the Forge CLI
version, allowing users to inspect manifests from older Forge releases
without upgrading.

BREAKING CHANGE: Manifests written by Forge 0.1.x are still readable,
but new manifests are written as v2. Users with automation that parses
v1 manifests must update their parsers to accept v2.

Migration guide: docs/migration/v1-to-v2-manifest.md
```

---

## Enforcement

The commit convention is enforced in three places:

### 1. Locally — pre-commit hook

The `conventional-pre-commit` hook runs on every `git commit` and rejects
invalid commit messages before they enter your history. This is your fastest
feedback loop.

To install the hook:

```bash
make install
```

To run it manually on a message you are about to commit:

```bash
echo "feat(cli): add list command" | uv run pre-commit run --hook-stage commit-msg conventional-pre-commit
```

### 2. In CI — PR title validation

The `.github/workflows/pr-lint.yml` workflow validates the **PR title** against
the same convention. Because Forge CLI uses **squash merges**, the PR title
becomes the commit message on `main` — so the PR title *is* the commit message.

If your PR title does not match the convention, CI fails and the PR cannot be
merged.

### 3. In review — maintainer review

Maintainers reject PRs with non-conventional titles. This is a courtesy, not a
gate — the CI check is the gate.

---

## Frequently Asked Questions

### "My change is a refactor *and* a fix. What type do I use?"

Use `fix`. The user-visible effect (a bug is fixed) matters more than the
implementation approach (a refactor). If the refactor is significant enough to
deserve its own commit, split the work into two PRs — one `refactor:`, then one
`fix:`.

### "My change touches three scopes. Which scope do I use?"

Omit the scope. A change that spans three areas is a change to the system, not
to a single module. `feat: add project manifest generation` is better than
`feat(cli,core,manifests): add project manifest generation`.

### "Do I need a body?"

For trivial changes (typo fixes, single-line fixes, dependency bumps), no. For
anything a reviewer would benefit from understanding, yes. Rule of thumb: if
you would explain it in a PR comment, put it in the body.

### "Can I use `feat!:` without a `BREAKING CHANGE:` footer?"

No. The `!` is a signal; the footer is the explanation. Always include both.
The footer is what tools and readers look for.

### "What if I forget the convention?"

The pre-commit hook catches it locally. If you bypass the hook with
`--no-verify`, CI catches it. If you bypass CI by force-merging, you have
bypassed the entire quality gate and need to reconsider your workflow.

### "What if my commit needs an emoji?"

Don't. Emojis in commit messages are inconsistent across terminal
implementations, complicate `grep`, and add no information. Use the type
prefix instead.

---

## Remembering the Convention

The whole convention fits in six one-line rules. Memorize these, and you will
never need to consult this page again.

1. **Type first** — `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, `style`, `revert`.
2. **Scope in parentheses** — if the change touches one module, name it.
3. **Imperative subject** — "add X", not "adds X" or "added X".
4. **Why in the body** — the diff shows *what*, the body explains *why*.
5. **Bang for breaking** — `feat!:` plus a `BREAKING CHANGE:` footer.
6. **Closes in the footer** — `Closes #123` auto-closes the issue on merge.

### The Feynman explanation, for the last time

> **Types are labels, not rules.** A `feat:` commit is not "more important"
> than a `docs:` commit — it is a different *kind* of change. Labels help tools
> and readers sort the history.
>
> **The scope is a zip code.** `feat(cli)` means "a feature in the CLI
> neighborhood." Without it, the address is too broad to find anything.
>
> **The subject is the front door.** It is what people see in `git log`. Make
> it inviting — imperative, lowercase, no period, under 72 characters.
>
> **The body is the letter inside.** It explains the *why* — the reason the
> front door exists. Without a body, the reader is left guessing.
>
> **The footer is the envelope.** It carries the issue references, the
> breaking-change notice, the co-authors. It is metadata, not narrative.
>
> **The `!` is a warning siren.** It says "this commit changes something that
> will break someone's day." The `BREAKING CHANGE:` footer is the *manual* that
> comes with the siren.

That is the whole convention. Everything else is details.

---

## Further Reading

- [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)
- [Semantic Versioning 2.0.0](https://semver.org/)
- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
- [How to Write a Git Commit Message](https://cbea.ms/git-commit/)
