# ADR Scripts

This directory contains the tooling that manages Architecture Decision Records
(ADRs) for Forge CLI. The ADRs themselves live in [`docs/adr/`](../../docs/adr/).
The scripts here generate and maintain the ADR index that lives alongside them.

If you are here to **create an ADR**, jump to [Creating an ADR](#creating-an-adr).
If you are here to **understand or modify the tooling**, read the whole page.

---

## Table of Contents

- [What is an ADR?](#what-is-an-adr)
- [Why this tooling exists](#why-this-tooling-exists)
- [Directory contents](#directory-contents)
- [Creating an ADR](#creating-an-adr)
- [The index](#the-index)
- [Workflow: from decision to merged PR](#workflow-from-decision-to-merged-pr)
- [Script reference](#script-reference)
- [Pre-commit integration](#pre-commit-integration)
- [CI integration](#ci-integration)
- [Troubleshooting](#troubleshooting)
- [Design decisions](#design-decisions)
- [Further reading](#further-reading)

---

## What is an ADR?

An Architecture Decision Record is a short document that captures a
significant architectural decision and its consequences. It answers the
question *"why is the system the way it is?"* — a question that code alone
cannot answer.

Each ADR has five sections:

1. **Status** — Proposed, Accepted, Deprecated, Superseded, or Rejected.
2. **Context** — the forces at play: technical, political, social, project-local.
3. **Decision** — what we decided, in active voice ("We will...").
4. **Consequences** — what gets easier, what gets harder, follow-up actions.
5. **Alternatives Considered** — what we rejected and why.

Forge CLI follows [Michael Nygard's format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions),
the de facto standard used by AWS, Google, Microsoft, and the Rust project.
You do not need to read Nygard's article to use this system — the
[template](../../docs/adr/template.md) is self-explanatory — but it is
worth reading once if you want to understand the reasoning.

---

## Why this tooling exists

A manual ADR workflow has one recurring failure mode: **the index drifts**.

Someone creates `docs/adr/0007-use-postgresql.md`. They forget to add it
to `docs/adr/README.md`. Six months later, the index lists five ADRs but
there are twelve in the directory. The index is worse than useless — it is
actively misleading, because it looks authoritative but is not.

This tooling eliminates that failure mode by **generating the index**:

1. `new.sh` creates a new ADR from the template and rebuilds the index.
2. `index.py` rebuilds the index from the ADR files.
3. A pre-commit hook runs `index.py` on every commit that touches `docs/adr/`.
4. A CI step runs `index.py --check` and fails if the index is stale.

The index cannot drift from the ADRs, because the index is rebuilt from
the ADRs. There is exactly one source of truth — the ADR files themselves.

---

## Directory contents

```
scripts/adr/
├── README.md      ← you are here
├── new.sh         ← creates a new ADR from the template
└── index.py       ← rebuilds docs/adr/README.md

docs/adr/
├── README.md      ← the index, auto-generated (do not edit by hand)
├── template.md    ← the ADR template
├── 0001-*.md      ← individual ADRs
└── ...
```

---

## Creating an ADR

### The 30-second version

```bash
make adr title="Use Typer for the CLI layer"
```

That is it. The script creates `docs/adr/NNNN-use-typer-for-the-cli-layer.md`,
fills in the number, date, and status, and rebuilds the index. Open the
file, fill in the sections, commit.

### Without `make`

```bash
bash scripts/adr/new.sh "Use Typer for the CLI layer"
```

`make` is a convenience wrapper around this exact command. If you are on
Windows without `make`, or in an environment where `make` is not available,
use the script directly.

### With options

```bash
# Specify the deciders (default: $USER)
bash scripts/adr/new.sh "Use Typer" --deciders "Team Platform"

# Start with a different status (default: Proposed)
bash scripts/adr/new.sh "Use Typer" --status Accepted

# Both
bash scripts/adr/new.sh "Use Typer" \
  --deciders "Team Platform" \
  --status Proposed
```

### What the script does

1. **Validates** the title is non-empty and contains at least one
   alphanumeric character.
2. **Finds the next number** by scanning `docs/adr/` for existing
   `NNNN-*.md` files and taking the highest number, plus one. Starts at
   `0001` if the directory is empty.
3. **Slugifies** the title: lowercase, ASCII-only, non-alphanumeric
   characters become hyphens, repeated hyphens collapse, leading and
   trailing hyphens are trimmed, capped at 60 characters.
4. **Copies** `docs/adr/template.md` and substitutes `{NUMBER}`, `{TITLE}`,
   `{STATUS}`, `{DATE}`, `{DECIDERS}`, `{CONSULTED}`, and `{INFORMED}`.
5. **Writes** the file to `docs/adr/NNNN-slug.md`.
6. **Refuses** to overwrite an existing ADR (exits 3 if the target exists).
7. **Rebuilds** the index by calling `index.py`.
8. **Prints** the path to the new file and next steps.

### Slugification examples

| Title | Slug | Filename |
| :--- | :--- | :--- |
| `Use Typer for the CLI layer` | `use-typer-for-the-cli-layer` | `0001-use-typer-for-the-cli-layer.md` |
| `Ruff v0.4 is the linter` | `ruff-v0-4-is-the-linter` | `0002-ruff-v0-4-is-the-linter.md` |
| `Café ordering (decision)` | `cafe-ordering-decision` | `0003-cafe-ordering-decision.md` |
| `Use PostgreSQL / SQLite` | `use-postgresql-sqlite` | `0004-use-postgresql-sqlite.md` |

Titles are slugified with `iconv` (to transliterate non-ASCII characters)
and `sed` (to replace non-alphanumeric characters). If `iconv` is not
available on your system, non-ASCII characters are dropped. The script
still succeeds as long as the resulting slug is non-empty.

---

## The index

`docs/adr/README.md` is **generated, never hand-edited**. It is rebuilt by
`index.py` from the ADR files. If you edit it by hand, the next commit will
overwrite your changes and exit 1 — the same "fix and re-stage" pattern
used by Ruff and `end-of-file-fixer`.

### Structure

The index contains:

1. A short introduction to ADRs.
2. A table with columns **Number**, **Title**, **Status**, and **Date**.
   Rows are sorted numerically by number (not lexically, so `0010` comes
   after `0009`).
3. A "Creating a new ADR" section with the `make adr` command.
4. A "Status values" table.
5. A link to the template.

### Rebuilding manually

```bash
make adr-index
```

Or:

```bash
python3 scripts/adr/index.py
```

### Verifying without writing

```bash
make adr-check
```

Or:

```bash
python3 scripts/adr/index.py --check
```

Exits 0 if the index is up to date, 1 if it is stale. This is the command
CI uses to block merges.

---

## Workflow: from decision to merged PR

```
1. Create the ADR
   └─ make adr title="Use Typer for the CLI layer"

2. Fill in the sections
   └─ Open docs/adr/0001-use-typer-for-the-cli-layer.md
   └─ Write Context, Decision, Consequences, Alternatives

3. Verify locally
   └─ make adr-check     (index must be up to date)
   └─ make check         (full quality gate)

4. Stage and commit
   └─ git add docs/adr/
   └─ git commit -s -m "docs(adr): 0001 use Typer for the CLI layer"

5. Push and open a PR
   └─ git push -u origin <branch>
   └─ Open PR against main

6. CI validates
   └─ make adr-check must pass, or the PR is blocked

7. Reviewer approves
   └─ The reviewer reads the ADR, not just the PR description

8. Merge
   └─ The PR is squash-merged (one commit per ADR)
```

---

## Script reference

### `new.sh`

Create a new ADR from the template.

**Usage:**

```
bash scripts/adr/new.sh "<title>" [--deciders "<names>"] [--status <status>]
```

**Arguments:**

| Argument | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `<title>` | Yes | — | The ADR title. Used in the filename and the document heading. |
| `--deciders` | No | `$USER` | Who made the decision. |
| `--status` | No | `Proposed` | The initial status. |
| `--help` / `-h` | No | — | Print usage and exit. |

**Exit codes:**

| Code | Meaning |
| :--- | :--- |
| `0` | Success. The ADR was created and the index was rebuilt. |
| `2` | Invalid input (empty title, missing template, invalid status). |
| `3` | The target file already exists. Refusing to overwrite. |

**Environment variables:**

| Variable | Default | Description |
| :--- | :--- | :--- |
| `ADR_DIR` | `docs/adr` | The directory containing ADRs. Override for testing. |

**Example:**

```bash
bash scripts/adr/new.sh "Use Typer for the CLI layer" --deciders "Team Platform"
```

---

### `index.py`

Rebuild `docs/adr/README.md` from the ADR files.

**Usage:**

```
python3 scripts/adr/index.py [--check] [--quiet]
```

**Arguments:**

| Argument | Description |
| :--- | :--- |
| `--check` | Verify the index is up to date without writing. Exits 1 on drift. |
| `--quiet` | Suppress the summary output. Errors still print to stderr. |

**Exit codes:**

| Code | Meaning |
| :--- | :--- |
| `0` | The index is up to date (or was successfully written). |
| `1` | The index was stale (write mode) or is stale (check mode). |
| `2` | The ADR directory does not exist. |

**Parsing rules:**

- Files matching `NNNN-*.md` are parsed. Everything else (e.g., `README.md`,
  `template.md`) is ignored.
- The title is read from the first `# NNNN. Title` heading. If absent, the
  title falls back to a title-cased version of the slug.
- The status is read from the `- **Status:** ...` line. If absent, the cell
  renders as `—`.
- The date is read from the `- **Date:** ...` line. If absent, the cell
  renders as `—`.

**Determinism:**

The output is deterministic. Running `index.py` twice on the same ADRs
produces byte-identical output. This is what allows `git diff` to be a
reliable drift signal.

**Example:**

```bash
# Rebuild and report
python3 scripts/adr/index.py
# ✅ ADR index regenerated (3 records).

# Verify only
python3 scripts/adr/index.py --check
# ✅ ADR index is up to date (3 records).
# (or)
# Error: docs/adr/README.md is out of date.
# Run 'python3 scripts/adr/index.py' to regenerate it.
# (exit 1)
```

---

## Pre-commit integration

`.pre-commit-config.yaml` includes a local hook that runs `index.py` on
every commit that touches `docs/adr/`:

```yaml
- repo: local
  hooks:
    - id: adr-index
      name: ADR index (rebuild)
      entry: python3 scripts/adr/index.py
      language: system
      files: ^docs/adr/.*\.md$
      pass_filenames: false
```

**How it behaves:**

- If the index is up to date, the hook passes silently.
- If the index is stale, the hook rebuilds it, exits 1, and prints a
  message. You must re-stage `docs/adr/README.md` and re-commit. This is
  the same "fix and re-stage" pattern used by Ruff and `end-of-file-fixer`.
- If `docs/adr/` is not touched by the commit, the hook is skipped
  entirely (the `files:` pattern is not matched).

**Why pre-commit and CI both check:**

- **Pre-commit** gives you fast feedback (milliseconds) and prevents
  stale indexes from entering local history.
- **CI** is the authoritative gate. It cannot be bypassed with
  `--no-verify`. It blocks merges.

The pre-commit hook is a convenience. CI is the contract.

---

## CI integration

`.github/workflows/ci.yml` includes a step in the `quality` job:

```yaml
- name: Verify ADR index is up to date
  run: make adr-check
```

**How it behaves:**

- If the index is up to date, the step passes.
- If the index is stale, the step fails with exit code 1, and the PR
  cannot be merged.
- The failure message points to `python3 scripts/adr/index.py`, so the
  fix is one command away.

**Why CI is the gate:**

The ruleset on `main` requires CI to pass before merging. A stale index
therefore cannot be merged, regardless of whether the contributor ran
the pre-commit hook locally.

---

## Troubleshooting

### "Error: template not found at docs/adr/template.md"

You are running `new.sh` from outside the repository root. The script
resolves paths relative to the current working directory. Run it from the
repository root:

```bash
cd /path/to/forge-cli
bash scripts/adr/new.sh "Your title"
```

Or set `ADR_DIR` to the absolute path:

```bash
ADR_DIR=/path/to/forge-cli/docs/adr bash scripts/adr/new.sh "Your title"
```

### "Error: could not derive a valid slug from the title"

Your title contains no ASCII alphanumeric characters. Use a title with at
least one letter or digit:

```bash
# Bad
bash scripts/adr/new.sh "!!!"

# Good
bash scripts/adr/new.sh "Use Typer for the CLI layer"
```

### "Error: docs/adr/NNNN-slug.md already exists. Refusing to overwrite."

An ADR with the same title was already created. Either edit the existing
one, or use a different title:

```bash
# If you meant to edit the existing ADR, open it
$EDITOR docs/adr/0001-use-typer-for-the-cli-layer.md

# If you meant to create a new one, use a different title
bash scripts/adr/new.sh "Use Typer 0.13 for the CLI layer"
```

### "Error: docs/adr/README.md is out of date."

The index is stale. Rebuild it:

```bash
make adr-index
```

Then re-stage and commit:

```bash
git add docs/adr/README.md
git commit -s
```

### Pre-commit hook says "fixes were made, please re-stage"

This is expected. The hook rebuilt the index and exited 1. Stage the
change and re-commit:

```bash
git add docs/adr/README.md
git commit -s
```

This is the same pattern as Ruff's `--fix` and `end-of-file-fixer`. It
is a feature, not a bug.

### The index does not list a new ADR

Check the filename. It must match `NNNN-*.md` exactly — four digits, a
hyphen, and the slug. Filenames like `1-use-typer.md` (one digit) or
`0001_use_typer.md` (underscore) are ignored by `index.py`.

### The status column shows `—` for an ADR

The ADR's status line is missing or malformed. The expected format is:

```markdown
- **Status:** Proposed
```

Check the file and fix the line.

---

## Design decisions

The scripts were designed around a small set of principles. Documenting
them here means future contributors can understand the "why" without
reading the code.

### The index is generated, never hand-edited

**Why:** A hand-edited index drifts from reality. A generated index cannot.
The cost of generating (one `python3` invocation) is far lower than the
cost of a stale index (misleading documentation, repeated corrections,
lost trust).

### The index generation exits 1 on drift

**Why:** Exit 1 is the signal that tells pre-commit and CI that the index
changed. If the script exited 0 on drift, the hook would pass silently,
and the developer would never review the regenerated table. Exit 1 forces
a review, which catches parsing bugs before they enter history.

### The ADR template follows Michael Nygard's format

**Why:** It is the de facto standard. Any reader who has seen one ADR
will recognize this one. Any tooling that works with Nygard-format ADRs
(e.g., `adr-tools`, Backstage's ADR plugin) will work with these.

### Filenames are `NNNN-slug.md`, not `slug.md`

**Why:** ADRs are read in order. The four-digit prefix makes that order
visible in every directory listing, every `ls`, every `git log`. It also
makes the index sortable by filename alone, without reading each file.

### The slug is capped at 60 characters

**Why:** Filenames that exceed 60 characters are truncated by many file
browsers, making them unreadable. 60 is long enough to be descriptive and
short enough to be legible.

### `new.sh` refuses to overwrite

**Why:** An ADR is a record of a decision. Overwriting it destroys the
record. If the user wants to revise a decision, they should create a
**new** ADR that supersedes the old one (per the ADR methodology). The
script enforces this by refusing to overwrite, which forces the user to
think about whether they are revising or replacing.

### `new.sh` calls `index.py` automatically

**Why:** Creating an ADR without updating the index is the exact bug this
tooling exists to prevent. Calling `index.py` from `new.sh` makes the two
operations atomic from the user's perspective — there is no way to create
an ADR and forget the index.

### `index.py` is idempotent

**Why:** Idempotency means running the script twice on the same input
produces byte-identical output. This is what allows `git diff` to be a
reliable drift signal. It also means the pre-commit hook can run on every
commit without churning the file.

---

## Further reading

- [Michael Nygard, "Documenting Architecture Decisions"](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
  — the original article that defined the format.
- [ADR GitHub organization](https://adr.github.io/) — the community
  hub for ADR tools and templates.
- [ThoughtWorks Technology Radar: Lightweight ADRs](https://www.thoughtworks.com/radar/techniques/lightweight-architecture-decision-records)
  — why ThoughtWorks recommends ADRs.
- [AWS Prescriptive Guidance: ADRs](https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/adr-process.html)
  — AWS's process for ADRs at scale.
- [The Rust RFC process](https://github.com/rust-lang/rfcs) — a
  superset of ADRs, if you want to see what comes next.
- [Forge CLI ADR template](../../docs/adr/template.md) — the template
  used by `new.sh`.
- [Forge CLI ADR index](../../docs/adr/README.md) — the generated index.
