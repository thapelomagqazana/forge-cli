# Architecture Decision Records

This directory contains the Architecture Decision Records (ADRs) for Forge CLI.

An ADR is a short document that captures a significant architectural decision
and its consequences. ADRs are immutable: once accepted, they are not edited.
If a decision changes, a new ADR supersedes the old one.

## Index

| Number | Title | Status | Date |
| :--- | :--- | :--- | :--- |
| [0001](0001-use-typer-for-the-cli-layer.md) | Use Typer for the CLI layer | Accepted | 2026-11-06 |
| [0002](0002-use-importlib-metadata-for-version-resolution.md) | Use importlib.metadata for version resolution | Accepted | 2026-11-06 |
| [0003](0003-use-a-file-based-command-registry.md) | Use a file-based command registry | Accepted | 2026-11-07 |
| [0004](0004-centralize-cli-output-and-error-handling.md) | Centralize CLI output and error handling | Accepted | 2026-11-07 |
| [0005](0005-stabilize-exit-codes-as-a-public-contract.md) | Stabilize exit codes as a public contract | Accepted | 2026-11-07 |
| [0006](0006-dual-mode-input-handling-with-a-single-validation-funnel.md) | Dual-Mode Input Handling with a Single Validation Funnel | Accepted | 2026-11-07 |
| [0007](0007-the-blueprint-json-schema-is-the-single-canonical-contract.md) | The Blueprint JSON Schema Is the Single Canonical Contract | Accepted | 2026-11-08 |
| [0008](0008-pydantic-models-are-a-generated-parity-tested-projection-of-.md) | Pydantic Models Are a Generated, Parity-Tested Projection of the Canonical Schema | Accepted | 2026-11-08 |
| [0009](0009-blueprint-discovery-uses-layered-sources-with-a-central-reso.md) | Blueprint Discovery Uses Layered Sources with a Central Resolver | Accepted | 2026-11-08 |
| [0010](0010-blueprint-loading-returns-a-result-and-aggregates-every-erro.md) | Blueprint Loading Returns a Result and Aggregates Every Error | Accepted | 2026-11-08 |
| [0011](0011-variable-resolution-is-a-pure-function-over-ordered-sources-.md) | Variable Resolution Is a Pure Function over Ordered Sources with a Single Validation Funnel | Accepted | 2026-11-09 |
| [0012](0012-cli-output-is-a-versioned-dto-consumed-by-pluggable-renderer.md) | CLI Output Is a Versioned DTO Consumed by Pluggable Renderers | Accepted | 2026-11-09 |

## Creating a new ADR

```bash
make adr title="Your decision title"
```

Or, if you do not have `make`:

```bash
bash scripts/adr/new.sh "Your decision title"
```

The script creates `docs/adr/NNNN-your-decision-title.md` from
[`template.md`](template.md), pre-filling the number, date, and status.

## Status values

| Status | Meaning |
| :--- | :--- |
| **Proposed** | Under discussion. Not yet binding. |
| **Accepted** | Approved and in effect. |
| **Deprecated** | No longer relevant, but not replaced. |
| **Superseded** | Replaced by a newer decision. Link to the new ADR. |
| **Rejected** | Considered and declined. Kept for historical record. |

## Template

The ADR template lives in [`template.md`](template.md). It follows
Michael Nygard's format, the de facto standard used by AWS, Google,
Microsoft, and the Rust project.
