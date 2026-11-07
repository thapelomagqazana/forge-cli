# Exit Codes

Forge CLI exits with one of the codes below. Scripts and CI
pipelines can branch on `$?` without parsing the message.

| Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | Success | The command completed. |
| `1` | User error | Invalid input, missing argument, or cancellation. |
| `2` | Blueprint error | A blueprint is missing, malformed, or incompatible. |
| `3` | Filesystem conflict | A target would be overwritten, or a path escapes the permitted root. |
| `4` | Verification failed | One or more required checks did not pass. |
| `5` | Internal error | An unexpected exception. This is a bug in Forge. |

The codes are chosen so that the **first digit** encodes the
severity:

- `0` — success.
- `1`–`3` — the user can fix this without help (bad input, missing
  blueprint, filesystem collision).
- `4` — the project was generated but is not verified.
- `5` — the tool is broken. Report it.

A script that wants "did the command succeed?" branches on `== 0`. A
script that wants "was it the user's fault?" branches on
`in (1, 2, 3)`. A script that wants "was it a bug?" branches on
`== 5`.

---

## What triggers each code

### `0` — Success

The command completed and did what the user asked.

**Examples:**

- `forge ping` printed `pong`.
- `forge --version` printed the version.
- `forge --help` printed the help.
- `forge list` printed the blueprint table.
- `forge create` created the project successfully (and, if
  `--verify` was passed, verified it successfully).
- `forge verify` ran all required checks and all of them passed.
- `forge doctor` ran all diagnostics and none failed.

### `1` — User error

The user did something Forge recognizes as invalid, or the user
cancelled:

- A missing required flag (`forge create python.fastapi.api`
  without `--name`).
- An invalid value (`--name "My Project"` — uppercase and spaces
  are not allowed).
- A cancellation (Ctrl+C, or a prompt the user declined).
- A missing argument (`forge create` without a `BLUEPRINT`).

The user can fix this. The error message will tell them how.

**Examples:**

```
$ forge create python.fastapi.api
✗ Missing argument 'BLUEPRINT'.
```

```
$ forge create python.fastapi.api --name "My Project"
✗ project name 'My Project' is invalid.
Hint: use a lowercase, hyphenated name (e.g., 'payments-api').
```

```
$ forge create python.fastapi.api --name my-api
^C
Cancelled.
```

### `2` — Blueprint error

The blueprint is missing, malformed, or incompatible with the
installed Forge:

- The blueprint ID is not in the registry.
- The blueprint's `blueprint.yaml` does not validate against the
  schema.
- The blueprint's `min_forge_version` is newer than the installed
  Forge.

This is also a user error, but the fix is *different* (choose a
different blueprint, upgrade Forge, fix the blueprint), so it gets
a different code. A script that branches on `2` can tell the user
"the blueprint you asked for cannot be used."

**Examples:**

```
$ forge create nonexistent.blueprint --name my-api
✗ blueprint 'nonexistent.blueprint' not found.
Hint: run 'forge list' to see available blueprints.
```

```
$ forge create python.fastapi.api --name my-api
✗ blueprint 'python.fastapi.api' requires Forge >= 2.0.0 (installed: 1.0.0).
Hint: upgrade Forge with 'uv tool upgrade forge-cli'.
```

### `3` — Filesystem conflict

A filesystem operation would overwrite or escape the permitted root:

- The target directory already exists and `--overwrite` was not
  passed.
- A template path escapes the staging directory.
- A permission error prevents writing to the target.

The user can fix this by choosing a different `--output`, passing
`--overwrite` (if they are certain), or fixing permissions.

**Examples:**

```
$ forge create python.fastapi.api --name my-api
✗ target directory ./my-api already exists.
Hint: choose a different --name, or pass --overwrite to replace it.
```

```
$ forge create python.fastapi.api --name my-api --output /root
✗ permission denied: cannot write to /root.
Hint: choose a writable --output directory, or check permissions.
```

### `4` — Verification failed

The project was generated (or the user ran `forge verify` on an
existing project), but one or more required checks did not pass:

- A required file is missing.
- The test suite fails.
- The linter reports errors.
- The Docker build fails.
- The CI workflow is invalid.

The user can fix this by addressing the failed checks. The error
message will tell them which check failed.

**Examples:**

```
$ forge verify
✗ verification failed: 2 checks did not pass.
Hint: run 'forge verify --check tests' or 'forge verify --check lint' for details.
```

Note that `forge verify` with a *skipped* check (e.g., Docker is not
installed) does **not** exit 4. Skips are reported as `SKIPPED`, not
`FAILED`, and do not fail the overall verification. Only *required*
checks that fail or error cause exit 4.

### `5` — Internal error

An unexpected exception. This is a **bug in Forge**, not a user
error. The user did nothing wrong.

The error message will point at the issue tracker. The user should
report the bug, ideally with the output of `forge --debug <command>`
(which shows the traceback).

**Examples:**

```
$ forge nonsense-command
✗ No such command 'nonsense-command'.
```

Wait — `forge nonsense-command` exits **2**, not 5. `UsageError` is a
Click exception, and the classifier treats Click exceptions as user
errors (exit 2). Exit 5 is reserved for *unexpected* exceptions —
bugs in Forge's own code.

The correct examples for exit 5:

```
$ forge create python.fastapi.api --name my-api
✗ An unexpected internal error occurred.
Hint: Run with --debug to see the full traceback, or report this at
https://github.com/thapelomagqazana/forge-cli/issues
```

An exit 5 should never happen in normal use. If it does, the user
should file a bug report.

---

## Stability

The exit codes are **part of the public CLI contract**. Changing
any of them is a breaking change and requires a major version bump
(`0.x` → `1.0` is exempt from this rule per Semantic Versioning, but
the codes are still expected to be stable within `0.x`).

Adding a *new* exit code is non-breaking (existing scripts don't
branch on it). Reusing an *existing* code for a different meaning is
breaking.

The codes are documented here and in `forge <command> --help`. A
change to either is a breaking change.

---

## Branching on exit codes

### Shell

```bash
#!/usr/bin/env bash
set -euo pipefail

if forge create python.fastapi.api --name "$PROJECT_NAME"; then
    echo "Project created."
else
    case $? in
        1) echo "User error: check your input." ;;
        2) echo "Blueprint error: the blueprint cannot be used." ;;
        3) echo "Filesystem conflict: choose a different output." ;;
        4) echo "Verification failed: the project has issues." ;;
        5) echo "Internal error: please report a bug." ;;
        *) echo "Unknown error: $?" ;;
    esac
    exit 1
fi
```

### Python

```python
import subprocess

result = subprocess.run(
    ["forge", "create", "python.fastapi.api", "--name", "my-api"],
    capture_output=True,
    text=True,
)

if result.returncode == 0:
    print("Project created.")
elif result.returncode == 1:
    print("User error:", result.stderr)
elif result.returncode == 2:
    print("Blueprint error:", result.stderr)
elif result.returncode == 3:
    print("Filesystem conflict:", result.stderr)
elif result.returncode == 4:
    print("Verification failed:", result.stderr)
elif result.returncode == 5:
    print("Internal error: please report a bug.")
    print(result.stderr)
else:
    raise SystemExit(f"Unexpected exit code: {result.returncode}")
```

### CI configuration (GitHub Actions)

```yaml
- name: Create project
  run: |
    set +e
    forge create python.fastapi.api --name my-api
    code=$?
    set -e
    if [ $code -ne 0 ]; then
      echo "forge create failed with exit code $code"
      exit $code
    fi
```

The `set +e` / `set -e` dance is necessary because GitHub Actions
runs `run:` blocks with `set -e` by default, which would exit on the
first failure before the `if` can inspect the code. The pattern above
captures the exit code explicitly, prints it, and then exits with it
— so the step's status matches the command's status.

---

## Distinguishing exit 1 from exit 2 from exit 3

All three are "the user can fix this," but the *fix* is different:

| Code | Fix | Who can fix |
| :--- | :--- | :--- |
| 1 | Re-run with valid input (check the flag, the value, the argument). | The user, without help. |
| 2 | Choose a different blueprint, upgrade Forge, or fix the blueprint. | The user, possibly with a maintainer's help. |
| 3 | Choose a different output, pass `--overwrite`, or fix permissions. | The user, possibly with a sysadmin's help. |

A script that wants "was it the user's fault?" branches on
`in (1, 2, 3)`. A script that wants "did the user pass bad input?"
branches on `== 1`. A script that wants "is the blueprint the
problem?" branches on `== 2`. A script that wants "is the
filesystem the problem?" branches on `== 3`.

---

## Distinguishing exit 4 from exit 5

Exit 4 is a *verification* failure: the project was generated, but
it has issues. The project is usable, but it does not meet the
baseline. The fix is to address the failed checks.

Exit 5 is an *internal* failure: Forge itself crashed. The project
was not generated (or was generated incorrectly). The fix is to
report a bug.

A script that wants "is the project usable?" branches on `== 4`
(the answer is "yes, but with warnings"). A script that wants "did
the command succeed?" branches on `== 0` (exit 4 is not success;
exit 5 is definitely not success).

---

## The exit code stability policy

The exit codes are stable within a minor version (`0.x` → `0.x+1`).
They are not stable across major versions (`0.x` → `1.0`,
`1.0` → `2.0`), though the project commits to *not* changing them
without a compelling reason, and to deprecating them with at least
one minor version of warning before removing them.

The codes are:

- **Documented** here, in `docs/reference/exit-codes.md`.
- **Tested** in `tests/unit/cli/test_errors.py::TestExitCodeMapping`.
- **Enforced** by `pyproject.toml`'s `[tool.pytest.ini_options]` and
  the classifier's contract.
- **Published** in `forge <command> --help` (the exit code contract
  is part of the command's help text).

A change to the codes requires:

1. A new ADR in `docs/adr/` that explains *why* the change is
   necessary and *what* the migration is.
2. An update to this document.
3. An update to the classifier and its tests.
4. A deprecation notice in the changelog.
5. A major version bump (per Semantic Versioning).

---

## Further reading

- [`streams.md`](streams.md) — where the error messages go
- [`../architecture/error-handling.md`](../architecture/error-handling.md)
  — how the codes are produced
- [`../architecture/cli-architecture.md`](../architecture/cli-architecture.md)
  — the CLI layer in detail
- [`../guides/adding-an-exception.md`](../guides/adding-an-exception.md)
  — how to add a new exit code
- [ADR 0004](../adr/0004-centralize-cli-output-and-error-handling.md)
  — the reasoning
