# Streams

Forge CLI writes to two streams: **stdout** and **stderr**. The split
is the Unix contract, and it is *strict*: a message belongs to
exactly one stream, and that stream is determined by the message's
*category*.

This document is the public contract for that split. Script authors
can rely on it to pipe, redirect, and parse Forge's output without
surprises.

If you are a contributor wondering *how* the split is enforced, see
[`../architecture/error-handling.md`](../architecture/error-handling.md)
(the classifier) and
[`../architecture/cli-architecture.md`](../architecture/cli-architecture.md)
(the `ui.py` module).

---

## The contract

| Category | Stream | Examples |
| :--- | :--- | :--- |
| **Product** | **stdout** | The result of `forge list --json`, the version printed by `forge --version`, the summary of a successful `forge create`. |
| **Commentary** | **stderr** | Error messages, warnings, hints, cancellations, progress bars, diagnostics. |

The rule is: **stdout is the product of the command; stderr is
commentary about the command.**

A script that pipes `forge list --json` to `jq` receives valid JSON
on stdout, with no error messages mixed in. If the command fails,
the error goes to stderr, and stdout is empty (or contains
whatever partial output was produced before the failure — see
[Partial output](#partial-output) below).

---

## Why the split matters

The split is not cosmetic. It is what makes Forge usable in shell
pipelines.

Consider this:

```bash
forge list --json | jq '.blueprints[].id'
```

If `forge list` writes the JSON to stdout (product) and any error
message to stderr (commentary), the pipeline works: `jq` receives
valid JSON, and the error message goes to the terminal, where the
user can see it.

If `forge list` wrote both to stdout, the pipeline would break: `jq`
would receive `{"blueprints": ...}\nError: ...`, which is not valid
JSON. `jq` would fail with a cryptic `parse error`, and the user
would blame `jq`, not Forge.

The split is what prevents this class of bug.

The same split is what makes `forge verify --json` usable in CI:

```bash
forge verify --json > verification-report.json
```

If the JSON goes to stdout, the report is valid. If an error
message went to stdout too, the report would be invalid, and the CI
pipeline would fail on the JSON parse — not on the actual
verification failure.

---

## What goes where

### stdout — product

Everything the user asked the command to *produce*:

- `forge --version` — the version string.
- `forge --help` — the help text.
- `forge list` — the blueprint table (or the JSON array with
  `--json`).
- `forge create` — the generation summary (destination, file count,
  next steps).
- `forge verify` — the verification summary (or the JSON object with
  `--json`).
- `forge doctor` — the diagnostic table (or the JSON object with
  `--json`).
- `forge ping` — the string `pong`.

### stderr — commentary

Everything the user did *not* ask for, but might want to know:

- Error messages (`✗ ...`).
- Warnings (`⚠ ...`).
- Hints (`Hint: ...`).
- Cancellations (`Cancelled.`).
- Progress bars, spinners, and other transient output.
- Diagnostics (from `--debug`).
- Test output (from `forge verify`'s check execution).

The principle is: **if a script would not want this in its parsed
input, it goes to stderr.**

---

## The `--json` mode

Every command that produces structured output supports `--json`. In
`--json` mode:

- **stdout** contains exactly one JSON object (or JSON array), on
  one or more lines, with no other text mixed in.
- **stderr** contains any error messages, warnings, or progress
  output.
- The exit code is the same as in human mode.

This means a script can do:

```bash
if forge verify --json > report.json 2> errors.log; then
    echo "Verification passed."
else
    echo "Verification failed. Report:"
    cat report.json
    echo "Errors:"
    cat errors.log
fi
```

The report is valid JSON. The errors are in a separate file. The
exit code tells you which branch to take.

### What "exactly one JSON object" means

Even when the command fails, stdout is valid JSON. The failure is
encoded in the JSON, not appended to it. For example:

```json
{
  "version": "1.0",
  "status": "error",
  "data": null,
  "errors": [
    {
      "code": "BLUEPRINT_NOT_FOUND",
      "message": "blueprint 'nonexistent.blueprint' not found.",
      "hint": "run 'forge list' to see available blueprints."
    }
  ]
}
```

The `status` is `"error"`. The `errors` array contains the failure.
The exit code is `2` (matching `BLUEPRINT_ERROR`). A script that
parses the JSON gets the error *and* the exit code, not one or the
other.

This is the difference between `--json` and human mode: in human
mode, the error is a message on stderr; in `--json` mode, the error
is *also* a field in the JSON. Both are valid; the script chooses
which one to use.

---

## Partial output

In human mode, a command may write partial output to stdout before
it fails. For example, `forge create` may print the "generating..."
progress line, then fail on a collision, then print the error to
stderr. The partial progress line remains on stdout.

In `--json` mode, this does not happen: either the full JSON object
is written to stdout (on success *or* failure), or nothing is
written to stdout at all (if the command crashes before producing
any JSON). A script that parses stdout can assume the JSON is
complete.

**If you need to know whether the command succeeded, use the exit
code, not the presence of output.** A command that writes output to
stdout does not mean it succeeded; the exit code is the signal.

---

## Parsing stdout

### Human-readable mode

The output is not designed for machine parsing. Do not `grep` it,
do not `awk` it, do not split it on whitespace. Use `--json`
instead.

The reason: human-readable output is a *courtesy* to the user. Its
format may change between minor versions if the change improves
readability. A script that parses it is a script that breaks the
moment the format changes.

### `--json` mode

The output is a single JSON object (or array) with a stable schema.
The schema is documented per-command:

- [`commands/list.md`](../commands/list.md) — the `forge list
  --json` schema.
- [`commands/create.md`](../commands/create.md) — the `forge
  create --json` schema.
- [`commands/verify.md`](../commands/verify.md) — the `forge
  verify --json` schema.
- [`commands/doctor.md`](../commands/doctor.md) — the `forge
  doctor --json` schema.

The schemas are versioned. Each JSON object has a `"version"` field
(e.g., `"1.0"`), which is independent of the Forge CLI version.
When the schema changes, the version increments.

A script can pin to a schema version:

```bash
forge verify --json | jq 'select(.version == "1.0") | .data'
```

If Forge ever produces a different version, the `jq` expression
returns nothing, and the script can fall back to a default or
error out. This is the contract: the schema version is the
compatibility signal.

---

## Parsing stderr

### Human-readable mode

stderr contains human-readable messages: error messages, warnings,
hints. It is not designed for machine parsing. Do not `grep` it
for specific error codes; use the exit code instead.

### `--json` mode

stderr contains progress output and diagnostic messages. The
*error* is in the JSON object on stdout, not on stderr. This is the
inverse of human mode: in human mode, the error is on stderr; in
`--json` mode, the error is in the JSON on stdout.

A script that wants "the error message" in `--json` mode reads the
`errors` array from the JSON, not from stderr. A script that wants
"the progress output" reads stderr.

---

## Redirecting streams

### Discarding stderr

```bash
forge verify --json 2>/dev/null
```

This discards stderr, leaving stdout clean for the JSON parser.
Useful when you trust the command to succeed and want the output
without progress messages.

### Discarding stdout

```bash
forge verify >/dev/null
```

This discards stdout, leaving stderr visible. Useful when you only
want to see whether the command failed and why.

### Writing to files

```bash
forge verify --json > report.json 2> progress.log
```

This writes stdout (the JSON) to `report.json` and stderr (progress)
to `progress.log`. The two streams are separate files, and the exit
code tells you which one to read.

### Combining streams

```bash
forge verify --json > combined.log 2>&1
```

This combines stdout and stderr into one file. **Do not do this if
you plan to parse the JSON** — the combined file contains both JSON
and progress output, and the JSON parser will fail.

If you need to combine for human consumption, `2>&1` is fine. If
you need to combine for parsing, extract the JSON first, then
combine the human output separately.

---

## Colors and formatting

Forge CLI uses colour and Unicode characters (`✓`, `⚠`, `✗`) for
human readability. These are *additive* — the message is readable
without them.

### Colour is disabled when

- stdout or stderr is not a TTY (i.e., when piped to another
  command).
- The `NO_COLOR` environment variable is set (to any value).
- The `--no-color` flag is passed (future — the flag lands with the
  configuration task).

### Colour is forced when

- The `FORCE_COLOR` environment variable is set (to any value).

### In `--json` mode

Colour is always disabled. The JSON is plain text, parseable by any
standard JSON parser.

If you are piping the output and still see colour codes, that is a
bug. Report it with the output of `env | grep COLOR` and the
command you ran.

---

## The stream contract is a promise

The split between stdout and stderr is *part of the public CLI
contract*. Changing it is a breaking change and requires a major
version bump.

Specifically:

- A message that goes to stdout today will not move to stderr in a
  minor version.
- A message that goes to stderr today will not move to stdout in a
  minor version.
- The `--json` output will remain valid JSON in every version.
- The `--json` output will not be mixed with human-readable
  messages.

If you are writing a script against Forge and the stream
behaviour changes in a way that breaks your script, that is a bug.
Report it at
[github.com/thapelomagqazana/forge-cli/issues](https://github.com/thapelomagqazana/forge-cli/issues).

---

## Further reading

- [`exit-codes.md`](exit-codes.md) — the exit-code contract
- [`../architecture/error-handling.md`](../architecture/error-handling.md)
  — how the classifier decides which stream
- [`../architecture/cli-architecture.md`](../architecture/cli-architecture.md)
  — the `ui.py` module and its stream helpers
- [`../guides/adding-a-command.md`](../guides/adding-a-command.md) —
  how to add a command (which uses `ui.print_*` for the right
  stream)
