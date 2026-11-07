# Adding an Exception

This guide walks you through adding a new error to Forge CLI. By the
end, you will have:

- A new `ForgeError` subclass (if the error is a new *kind* of
  failure), or a new `raise` of an existing subclass (if the error
  is an instance of an existing kind).
- A distinct exit code.
- A hint that tells the user what to do next.
- Tests that verify the rendering, the exit code, and the hint.
- A `make check` that passes.

The guide takes about 10 minutes to complete.

If you are only looking for a *reference* of the exception hierarchy,
see [`src/forge/core/exceptions.py`](../../src/forge/core/exceptions.py).

If you are thinking about adding a *fifth category* to the
classifier (not just a new exception, but a new *kind* of
classification), stop and read
[`../architecture/error-handling.md`](../architecture/error-handling.md)
first. Most "new exceptions" are instances of the four existing
categories, and the answer is to reuse a subclass, not to add one.

---

## Before you start

You will need:

- A working development environment. Run `make install` if you have
  not already. See [`development-setup.md`](development-setup.md).
- A clean working tree. Run `git status` and commit or stash any
  in-progress changes.
- A clear idea of *what kind* of failure your exception represents.

The last one is the most important. Before writing any code, answer
this question:

> **Is this exception a new *kind* of failure, or a new *instance*
> of an existing kind?**

If the answer is **instance**, you do not need a new exception class.
You need to raise an existing subclass with an appropriate message
and hint. For example:

```python
raise UserInputError(
    "project name must be lowercase",
    hint="try --name payments-api instead of --name Payments-API",
)
```

If the answer is **kind**, you need a new `ForgeError` subclass.
Examples of "kinds": a `RateLimitError` (a new category of failure
that deserves its own exit code), a `NetworkError` (a new category
for registry-related failures). The bar for a new kind is *high* —
the four existing kinds cover the vast majority of user-facing
failures.

This guide covers both cases. Step 2 handles the "kind" case; Step 3
handles the "instance" case. The rest of the guide is the same for
both.

---

## Step 1: Identify the category

Every `ForgeError` belongs to one of four categories, and the
category determines the exit code. Ask yourself: *what kind of
failure is this?*

| Category | When to use | Exit code |
| :--- | :--- | :--- |
| `UserInputError` | The user passed invalid input (bad flag, missing argument, invalid value). | 1 |
| `BlueprintError` | A blueprint is missing, malformed, or incompatible with the installed Forge. | 2 |
| `FilesystemConflictError` | A filesystem operation would overwrite or escape the permitted root. | 3 |
| `VerificationError` | One or more verification checks did not pass. | 4 |

If your failure fits one of these categories, **you do not need a
new subclass** — you need to raise the existing subclass (Step 3).

If your failure does not fit any of these categories, **you might
need a new subclass**. But before you add one, ask:

- Is this really a new *kind* of failure, or is it a `UserInputError`
  with a specific message?
- Would a user distinguish this failure from the existing
  categories, or would they treat it as "the same kind of thing"?
- Does this failure deserve its own *exit code*, or could it share
  an exit code with an existing category?

If the answer to the last question is "no, it could share an exit
code", **you do not need a new subclass** — you need a distinct
*message* on an existing one. The exit code is the contract; the
message is the detail.

For example, "the user passed an invalid project name" and "the user
passed an invalid blueprint ID" are both `UserInputError` — they are
different messages, but the same category. A script branching on
`$?` does not care *which* input was invalid; it cares that the user
made an input error. The exit code is `1` for both.

A new subclass is justified when the failure is a *different kind of
thing* — when a script branching on `$?` would want to distinguish
it. For example: "the user hit a rate limit" is different from "the
user passed invalid input" — a script might want to retry on rate
limit but not on bad input. That justifies a new subclass
(`RateLimitError`, exit code 5 or a new code).

**When in doubt, do not add a new subclass.** Raise an existing one
with a clear message and hint. Adding a subclass is cheap now and
expensive later (every subclass is a public API promise, an exit
code commitment, a documented category).

---

## Step 2: Add a new subclass (only if Step 1 says so)

If you have confirmed that you need a new subclass, add it to
`src/forge/core/exceptions.py`:

```python
class RateLimitError(ForgeError):
    """The registry rate-limited the request.

    The user (or their CI system) should wait and retry. The
    exception carries a ``retry_after`` attribute with the number
    of seconds to wait, when known.

    This is a new category (not a user error, not a filesystem
    conflict) because a script may want to retry automatically,
    and the wait time is part of the contract.
    """

    exit_code = ExitCode.INTERNAL_ERROR  # 5, for now
    retry_after: int | None = None

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.retry_after = retry_after
```

**The subclass must:**

1. **Live in `core/exceptions.py`.** Not in a command module, not in
   a separate file. The exception hierarchy is one file because it is
   one contract.
2. **Inherit from `ForgeError`.** Not from `Exception` directly. The
   classifier only recognizes `ForgeError` subclasses.
3. **Declare a distinct `exit_code`.** If you give it the same code
   as an existing subclass, the classifier cannot distinguish them
   — which defeats the purpose of a new subclass. (If you *want* the
   same code, see Step 1: you probably want a message, not a
   subclass.)
4. **Have a docstring** that explains *why* the subclass exists, and
   *what* distinguishes it from the existing four.
5. **Carry extra attributes if the failure has them.** For example,
   `RateLimitError` carries `retry_after` — the number of seconds to
   wait. The extra attribute is part of the subclass's contract, and
   the CLI can read it to produce a better hint.

**Do not:**

- Add a subclass "just in case." Every subclass is a public API
  promise. Removing one later is a breaking change.
- Reuse an existing exit code "because it's close enough." Distinct
  codes are why subclasses exist. If the codes are the same, the
  subclasses should be one.
- Forget the docstring. A subclass without a docstring is a subclass
  that future contributors will not understand, and will not use.

**Update the exit-code reference.** Add the new code to
`docs/reference/exit-codes.md`:

```markdown
| Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | Success | ... |
| `1` | User error | ... |
| `2` | Blueprint error | ... |
| `3` | Filesystem conflict | ... |
| `4` | Verification failed | ... |
| `5` | Internal error | ... |
| `6` | Rate limit | The registry rate-limited the request; retry later. |
```

**Update the `ExitCode` enum** if you added a new code:

```python
class ExitCode(IntEnum):
    """Documented exit codes for the ``forge`` command."""

    SUCCESS = 0
    USER_ERROR = 1
    BLUEPRINT_ERROR = 2
    FILESYSTEM_CONFLICT = 3
    VERIFICATION_FAILED = 4
    INTERNAL_ERROR = 5
    RATE_LIMIT = 6  # new
```

**Update the classifier.** If the new subclass needs a new *branch*
in the classifier (i.e., it needs different rendering than the
generic `ForgeError` branch), update `cli/errors.py`. Otherwise, the
generic `ForgeError` branch handles it — no classifier change needed.

The `RateLimitError` example above does **not** need a new classifier
branch: it is a `ForgeError`, it renders like the other subclasses,
it just has a different exit code. The classifier's `ForgeError`
branch handles it.

**Update the tests.** Add a parametrized case to
`TestForgeError::test_each_subclass_uses_its_declared_exit_code`:

```python
@pytest.mark.parametrize(
    ("exc_class", "expected_code"),
    [
        (UserInputError, ExitCode.USER_ERROR),
        (BlueprintError, ExitCode.BLUEPRINT_ERROR),
        (FilesystemConflictError, ExitCode.FILESYSTEM_CONFLICT),
        (VerificationError, ExitCode.VERIFICATION_FAILED),
        (ForgeError, ExitCode.INTERNAL_ERROR),
        (RateLimitError, ExitCode.RATE_LIMIT),  # new
    ],
)
```

And add the subclass to `TestExitCodeMapping::test_all_subclasses_have_distinct_codes`:

```python
subclasses = [
    UserInputError,
    BlueprintError,
    FilesystemConflictError,
    VerificationError,
    RateLimitError,  # new
]
```

Run the tests:

```bash
make test
```

If the new subclass is correctly wired, all tests pass. If not, the
failure message tells you which assertion failed.

---

## Step 3: Raise the exception

Whether you are raising a new subclass (Step 2) or an existing one,
the raise site is the same: in a command, in a service, or in a
utility function.

**Raise early, raise specific.** The exception should be raised at
the point where the failure is *detected*, not at the point where it
is *reported*. This keeps the call site simple (one `raise` line) and
the stack trace clean (if `--debug` is passed).

**Include a message that says what went wrong.** The message is the
first line the user sees. It should be imperative and specific:

- ✅ `"project name must be lowercase"`
- ✅ `"blueprint 'python.fastapi.api' not found"`
- ❌ `"invalid input"`
- ❌ `"something went wrong"`

**Include a hint that says what to do next.** The hint is rendered
on a separate, dimmed line. It should be a one-line remediation
suggestion, starting with a verb:

- ✅ `"try --name payments-api instead of --name Payments-API"`
- ✅ `"run 'forge list' to see available blueprints"`
- ✅ `"delete the existing directory or pass --overwrite"`
- ❌ `"check your input"` (too vague)
- ❌ `"this might be a permissions issue"` (not actionable)

**Do not `sys.exit` or `print_error`.** The classifier handles both.
Raise the exception and let it propagate to `main()`, where
`handle_exception` renders it and exits with the right code.

Here is a worked example of raising an exception from a command:

```python
"""``forge create`` — create a project from a blueprint."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from forge.cli.ui import print_success
from forge.core.exceptions import (
    BlueprintError,
    FilesystemConflictError,
    UserInputError,
)

app = typer.Typer(
    name="create",
    help="Create a project from a blueprint.",
)


@app.command()
def create(
    blueprint: Annotated[
        str,
        typer.Argument(
            help="Blueprint ID (e.g., 'python.fastapi.api').",
        ),
    ],
    name: Annotated[
        str,
        typer.Option(
            "--name",
            "-n",
            help="Project name. Must be lowercase and hyphenated.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination directory. Defaults to the current directory.",
        ),
    ] = Path("."),
) -> None:
    """Create a new project from a blueprint."""
    # Validate the project name.
    if not name.islower() or " " in name:
        raise UserInputError(
            f"project name {name!r} is invalid.",
            hint="use a lowercase, hyphenated name (e.g., 'payments-api').",
        )

    # Check the target directory.
    target = output / name
    if target.exists():
        raise FilesystemConflictError(
            f"target directory {target} already exists.",
            hint="choose a different --name, or delete the directory first.",
        )

    # Load the blueprint (placeholder; the real loader lands later).
    blueprint_loader = _get_blueprint_loader()
    if not blueprint_loader.exists(blueprint):
        raise BlueprintError(
            f"blueprint {blueprint!r} not found.",
            hint="run 'forge list' to see available blueprints.",
        )

    # ... generation pipeline (future) ...

    print_success(f"project {name!r} created at {target}")
```

Notice:

- **The message is specific.** `"project name 'Payments-API' is invalid."` — the user sees exactly what went wrong.
- **The hint is actionable.** `"use a lowercase, hyphenated name..."` — the user knows what to do next.
- **The exception type is the right category.** `UserInputError` for invalid input, `FilesystemConflictError` for a collision, `BlueprintError` for a missing blueprint.
- **The command does not catch the exceptions.** It lets them propagate to `main()`, where the classifier renders them.
- **The success path uses `print_success`.** The failure path does not print at all — the classifier prints.

---

## Step 4: Add tests

Every new exception needs tests. The tests live in
`tests/unit/cli/test_errors.py` (for the classifier's rendering) and
in the command's test file (for the raise site).

### Tests for the classifier

If you added a new subclass (Step 2), add a parametrized case to
`TestForgeError::test_each_subclass_uses_its_declared_exit_code`:

```python
@pytest.mark.parametrize(
    ("exc_class", "expected_code"),
    [
        (UserInputError, ExitCode.USER_ERROR),
        (BlueprintError, ExitCode.BLUEPRINT_ERROR),
        (FilesystemConflictError, ExitCode.FILESYSTEM_CONFLICT),
        (VerificationError, ExitCode.VERIFICATION_FAILED),
        (ForgeError, ExitCode.INTERNAL_ERROR),
        (RateLimitError, ExitCode.RATE_LIMIT),  # new
    ],
)
```

And add the subclass to `TestExitCodeMapping::test_all_subclasses_have_distinct_codes`:

```python
subclasses = [
    UserInputError,
    BlueprintError,
    FilesystemConflictError,
    VerificationError,
    RateLimitError,  # new
]
```

If the new subclass has extra attributes (like `retry_after`), add a
test for the rendering of those attributes:

```python
@pytest.mark.unit()
def test_rate_limit_renders_retry_after(
    self, captured: io.StringIO
) -> None:
    """A ``RateLimitError`` with ``retry_after`` renders the wait time."""
    exc = RateLimitError(
        "registry rate limit exceeded.",
        hint="wait and retry.",
        retry_after=30,
    )
    with pytest.raises(SystemExit):
        errors.handle_exception(exc, debug=False)

    output = captured.getvalue()
    assert "rate limit" in output.lower()
    # Note: the classifier does not know about ``retry_after`` — it
    # only renders the message and hint. If you want the retry time
    # in the output, put it in the message (e.g., "wait 30 seconds
    # and retry") or in the hint.
```

**A note on extra attributes.** The classifier's `ForgeError` branch
only renders `str(exc)` and `exc.hint`. If your subclass has an extra
attribute (like `retry_after`), the classifier will not render it
automatically — the attribute is *data*, not a message. If you want
the data in the output, either:

1. Include it in the message (`"wait 30 seconds and retry"`), or
2. Include it in the hint (`"retry after 30 seconds"`), or
3. Add a new branch to the classifier that renders the attribute
   (this is a bigger change; see `architecture/error-handling.md`).

For most subclasses, option 1 or 2 is enough. Option 3 is for
subclasses whose *entire point* is the extra data (like a
structured rate-limit error that a script wants to parse).

### Tests for the raise site

Add a test to the command's test file that exercises the error path:

```python
@pytest.mark.unit()
class TestCreate:
    """Tests for the ``forge create`` command."""

    def test_invalid_project_name_exits_with_user_error(
        self, cli_runner, forge_app
    ) -> None:
        """An invalid project name raises UserInputError, exits 1."""
        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api", "--name", "Payments-API"],
        )
        assert result.exit_code == 1
        assert "invalid" in result.output.lower()

    def test_target_directory_exists_exits_with_conflict(
        self, cli_runner, forge_app, tmp_path
    ) -> None:
        """An existing target raises FilesystemConflictError, exits 3."""
        existing = tmp_path / "my-project"
        existing.mkdir()
        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-project",
                "--output",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 3
        assert "already exists" in result.output.lower()

    def test_missing_blueprint_exits_with_blueprint_error(
        self, cli_runner, forge_app
    ) -> None:
        """A missing blueprint raises BlueprintError, exits 2."""
        result = cli_runner.invoke(
            forge_app,
            ["create", "nonexistent.blueprint", "--name", "my-project"],
        )
        assert result.exit_code == 2
        assert "not found" in result.output.lower()
```

Run the tests:

```bash
make test
```

---

## Step 5: Update the documentation

If the new subclass is user-facing (i.e., it produces an exit code
that users or scripts will branch on), update
`docs/reference/exit-codes.md` with the new code and its meaning.

If the new subclass has a special hint format or extra attributes,
document them in the subclass's docstring — that is the primary
documentation.

If the new subclass is *only* used internally (not exposed to users
as a distinct exit code), no external documentation is needed — the
docstring is enough.

---

## Step 6: Run the full gate

```bash
make check
```

This runs lint, format, type, test, security, and ADR check. Every
one must pass.

The most common failures when adding an exception:

| Failure | Cause | Fix |
| :--- | :--- | :--- |
| `ruff` fails | Missing import, unused variable, style violation | Fix the code, then run `make fix` to auto-fix formatting. |
| `mypy` fails | Missing type annotation on `__init__` or attributes | Add the annotation. |
| `pytest` fails | Test does not match the behavior | Fix the code (or the test, if the test is wrong — but usually the code). |
| Coverage < 90% | The new subclass or its `__init__` is not tested | Add tests for the new code paths. |

---

## Step 7: Commit

```bash
git add src/forge/core/exceptions.py \
        src/forge/cli/errors.py \
        docs/reference/exit-codes.md \
        tests/unit/cli/test_errors.py
git commit -s -m "feat(core): add RateLimitError for registry rate limits

Add a new ForgeError subclass that represents a rate-limited
response from the registry. The subclass carries a retry_after
attribute with the number of seconds to wait, when known.

The exit code is 6 (RATE_LIMIT), distinct from the existing
categories, so scripts can retry on rate limit but not on user
input or filesystem errors.

The classifier's ForgeError branch handles the new subclass
automatically — no classifier changes needed. The subclass's
__init__ is tested in test_rate_limit_renders_retry_after, and the
exit code is tested in the parametrized
test_each_subclass_uses_its_declared_exit_code.

The docs/reference/exit-codes.md table is updated with the new
code and its meaning."
```

Once committed, your exception is part of Forge CLI. The classifier
recognizes it. The tests cover it. The exit-code reference documents
it. Future contributors will find it in the hierarchy and know how to
use it.

---

## Checklist

Before you open a PR, confirm:

- [ ] The failure is a *new kind* of failure, not a new instance of
      an existing kind. (If it is an instance, use an existing
      subclass.)
- [ ] The new subclass inherits from `ForgeError`.
- [ ] The new subclass has a *distinct* `exit_code`.
- [ ] The new subclass has a docstring that explains why it exists.
- [ ] The new subclass has extra attributes (like `retry_after`) if
      the failure carries data.
- [ ] The `__init__` signature is documented with type annotations.
- [ ] The `ExitCode` enum has the new code (if applicable).
- [ ] The `docs/reference/exit-codes.md` table is updated.
- [ ] The raise site uses the new subclass with a specific message
      and an actionable hint.
- [ ] The raise site does *not* use `sys.exit` or `print_error`.
- [ ] Tests cover the classifier's rendering of the new subclass.
- [ ] Tests cover the raise site's error path.
- [ ] `make check` passes.

---

## Further reading

- [`../architecture/error-handling.md`](../architecture/error-handling.md)
  — the classifier's contract, the four categories, why `--debug`
  is selective
- [`../reference/exit-codes.md`](../reference/exit-codes.md) — the
  public exit-code contract
- [`../reference/streams.md`](../reference/streams.md) — the
  stdout/stderr contract
- [`adding-a-command.md`](adding-a-command.md) — how to add a command
  (which is where most exceptions are raised)
- [`src/forge/core/exceptions.py`](../../src/forge/core/exceptions.py)
  — the exception hierarchy
- [`src/forge/cli/errors.py`](../../src/forge/cli/errors.py) — the
  classifier
