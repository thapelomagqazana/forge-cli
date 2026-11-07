# Running Tests

This guide covers the test workflow: how to run the tests, how to
read the output, how to run a single test, and how to add a new one.

If you are only looking for the *fast* answer:

```bash
make test     # run the full suite
make check    # run the full quality gate (tests + lint + types + security)
```

The rest of this guide explains *why* those commands exist, *when*
to use which one, and *what to do* when a test fails.

---

## The test strategy

Forge CLI has three kinds of tests, distinguished by their scope
and their speed:

| Kind | Location | Speed | Purpose |
| :--- | :--- | :--- | :--- |
| **Unit** | `tests/unit/` | < 1s | Test a single function or class in isolation. |
| **Integration** | `tests/integration/` | 1–10s | Test multiple components together (e.g., a command + the classifier). |
| **End-to-end** | `tests/e2e/` | 10–60s | Test the full CLI via `subprocess`. |

Most tests are unit tests. Integration tests are rare. E2E tests
are rarer still — they are expensive (they spawn a subprocess) and
they are the ones most likely to be flaky (they depend on the
environment).

The test pyramid is the strategy:

```
          ╱╲
         ╱  ╲           E2E (rare)
        ╱    ╲
       ╱──────╲
      ╱        ╲        Integration (occasional)
     ╱──────────╲
    ╱            ╲      Unit (the majority)
   ╱______________╲
```

The pyramid is not arbitrary. Unit tests are fast and stable, so
they can be run on every save. Integration and E2E tests are slow
and occasionally flaky, so they are run less frequently.

---

## The commands

### The full suite: `make test`

```bash
make test
```

This runs every test in `tests/`, with coverage reporting. It is the
command you run before pushing, and the command CI runs.

**Expected output:**

```
..............................s............................
---------- coverage: platform linux, python 3.12.3-final-0 -----------
Name                                 Stmts   Miss Branch BrPart   Cover   Missing
---------------------------------------------------------------------------------
src/forge/cli/errors.py                 24      0     10      0 100.00%
...
TOTAL                                  287     20     48      3  91.94%
Required test coverage of 90% reached. Total coverage: 91.94%
137 passed, 1 skipped in 2.13s
```

The `137 passed` is the number of tests that passed. The `1 skipped`
is a test that is explicitly skipped (usually because it needs a
real package on disk). The `91.94%` is the coverage — it must be at
least 90% or the command fails.

### The full gate: `make check`

```bash
make check
```

This runs the full quality gate: lint, format check, type check,
tests, security, and ADR check. It is the command CI runs, and the
command you should run before opening a PR.

**Expected output:**

```
uv run ruff check --exit-non-zero-on-fix .
All checks passed!
uv run ruff format --check .
36 files already formatted
uv run mypy src
Success: no issues found in 24 source files
uv run pytest
... (test output) ...
==> Bandit (static analysis)...
==> pip-audit (dependency vulnerabilities)...
✅ All quality gates passed.
```

The `✅ All quality gates passed.` line is the signal: every check
passed. If any check fails, the command exits non-zero and prints
the failure.

### A single test file: `make test-file`

```bash
make test-file FILE=tests/unit/cli/test_errors.py
```

This runs one test file, with `--no-cov` (no coverage gate). It is
the fast inner loop: it takes under a second, and it does not fail
because the *total* coverage of the project dropped.

**Why `--no-cov`?** Because coverage is a property of the *full
suite*, not of a single file. Running one file exercises one module,
which drops total coverage well below 90% even though the file's
own tests pass. The `--no-cov` flag bypasses the gate for the
single-file run. The gate is still enforced by `make test` and
`make check`.

### A single test: `pytest -k`

```bash
uv run pytest -k "test_errors and cancellation" -v --no-cov
```

This runs every test whose name contains "test_errors" *and*
"cancellation". It is the fastest way to run a *specific* test when
you know its name.

### A single test by node ID: `pytest <path>::<class>::<test>`

```bash
uv run pytest tests/unit/cli/test_errors.py::TestCancellation::test_keyboard_interrupt_renders_cancelled -v --no-cov
```

This runs *exactly one* test. It is the most precise invocation.

### Run the tests with coverage for a specific module

```bash
uv run pytest tests/unit/cli/test_errors.py \
    --cov=src/forge/cli/errors \
    --cov-report=term-missing --no-cov-on-fail
```

This reports coverage for `errors.py` only, which is useful when you
are testing that file and want to see which branches are not
covered.

### Run the tests with a specific marker

```bash
uv run pytest -m unit --no-cov
uv run pytest -m integration --no-cov
uv run pytest -m "not slow" --no-cov
```

Markers are declared in `pyproject.toml`. The available markers are:

| Marker | Meaning |
| :--- | :--- |
| `unit` | A fast, isolated test. |
| `integration` | A test that touches the filesystem or a subprocess. |
| `contract` | A test that validates a schema or an output contract. |
| `e2e` | A test that invokes the full CLI via `subprocess`. |
| `security` | A test for path traversal, injection, or another security concern. |
| `slow` | A slow test (deselect with `-m "not slow"`). |

Every test *must* have a marker. `pytest` is configured with
`--strict-markers`, so a test without a marker will fail at
collection time.

---

## Reading the output

### The test results

The line of dots (`.`) is the test progress. Each dot is a passing
test. The letters mean:

| Letter | Meaning |
| :--- | :--- |
| `.` | Passed. |
| `F` | Failed. |
| `E` | Errored (the test itself raised an exception during setup or teardown). |
| `s` | Skipped. |
| `x` | Xfailed (expected to fail, and it did). |
| `X` | Xpassed (expected to fail, but it passed — usually a problem). |

### The failure message

When a test fails, pytest prints the full failure:

```
FAILED tests/unit/cli/test_errors.py::TestCancellation::test_keyboard_interrupt_renders_cancelled
======================================================================== FAILURES =========================================================================
___________________________ TestCancellation.test_keyboard_interrupt_renders_cancelled ___________________________

    @pytest.mark.unit()
    def test_keyboard_interrupt_renders_cancelled(self, captured: io.StringIO) -> None:
        """``KeyboardInterrupt`` renders "Cancelled." and exits 1."""
        with pytest.raises(SystemExit) as exc_info:
            errors.handle_exception(KeyboardInterrupt(), debug=False)

        assert exc_info.value.code == ExitCode.USER_ERROR
        output = captured.getvalue()
>       assert "Cancelled" in output
E       AssertionError: assert 'Cancelled' in ''
E        +  where '' = <built-in method getvalue of _io.StringIO object at 0x7f...>

tests/unit/cli/test_errors.py:205: AssertionError
```

Read it top-down:

1. **The failing test's name** — `test_keyboard_interrupt_renders_cancelled`.
2. **The test's source** — the function body, with the failing line marked (`>`).
3. **The assertion** — `assert 'Cancelled' in ''`. The assertion failed because the captured output was empty.
4. **The context** — the `captured` buffer was empty. This tells you *why* the assertion failed: not that "Cancelled" was in a non-empty string, but that the string was empty. The handler did not write anything to the captured buffer.

The failure message is the *diagnostic*. It tells you exactly which
line failed, what the actual value was, and (implicitly) what the
expected value was.

### The coverage report

The coverage report lists every module, its number of statements,
the number missed, the branch coverage, and the missing lines:

```
Name                                 Stmts   Miss Branch BrPart   Cover   Missing
---------------------------------------------------------------------------------
src/forge/cli/errors.py                 24      0     10      0 100.00%
src/forge/cli/ui.py                     49      1      4      1  96.23%   82
```

The `Missing` column tells you which lines were not executed by the
tests. Line `82` in `ui.py` was not executed. That is a hint: the
test suite does not cover line 82.

To see what line 82 is:

```bash
sed -n '82p' src/forge/cli/ui.py
```

You will likely find a `try/except` branch, a broken-pipe fallback,
or another edge case that the happy-path tests do not exercise. Add
a test that covers it.

### The coverage gate

If coverage is below 90%, the command fails:

```
FAIL Required test coverage of 90% not reached. Total coverage: 88.52%
```

The failure does *not* mean the tests failed — they may all have
passed. It means the *coverage* is below the gate. The fix is to add
tests for the uncovered lines, not to lower the gate.

See [Why not lower the gate?](#why-not-lower-the-gate) below.

---

## Adding a new test

### Where to add it

Add the test to the file that tests the module:

| You are testing | Add the test to |
| :--- | :--- |
| `cli/main.py` | `tests/unit/cli/test_main.py` |
| `cli/registry.py` | `tests/unit/cli/test_registry.py` |
| `cli/ui.py` | `tests/unit/cli/test_ui.py` |
| `cli/errors.py` | `tests/unit/cli/test_errors.py` |
| `core/version.py` | `tests/unit/core/test_version.py` |
| `core/config.py` | `tests/unit/core/test_config.py` |

If the module does not exist yet (e.g., a future `forge.generation`
module), create a new file that mirrors the module's path.

### How to structure it

A test is a function whose name starts with `test_`, decorated with
a marker:

```python
@pytest.mark.unit()
def test_something() -> None:
    """One-line description of what the test proves."""
    # Arrange
    # Act
    # Assert
```

The pattern is **Arrange / Act / Assert**:

- **Arrange**: set up the inputs.
- **Act**: call the code under test.
- **Assert**: check the output.

For example:

```python
@pytest.mark.unit()
def test_print_error_writes_to_stderr() -> None:
    """``print_error`` writes to stderr, not stdout."""
    # Arrange
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    # (replace the consoles with in-memory ones — see test_ui.py)

    # Act
    ui.print_error("boom")

    # Assert
    assert "boom" in stderr_buf.getvalue()
    assert "boom" not in stdout_buf.getvalue()
```

### How to name it

The name should describe *what the test proves*, not *what the test
does*. A test named `test_print_error_writes_to_stderr` is better
than `test_print_error_1` or `test_prints_error`.

The pattern is: `<what is tested>_<what it proves>`. Examples:

- `test_print_error_writes_to_stderr`
- `test_forge_error_renders_message_and_hint`
- `test_debug_re_raises_unexpected_exception`
- `test_registry_skips_module_without_app`

### How to use fixtures

Fixtures are defined in `tests/conftest.py` (shared) or in the test
file (local). The shared fixtures are:

| Fixture | What it provides |
| :--- | :--- |
| `cli_runner` | A `Typer.CliRunner` for invoking the CLI in-process. |
| `forge_app` | A fresh `typer.Typer` app instance. |
| `forge_executable` | The command prefix for subprocess tests (`forge` or `python -m forge.cli`). |
| `isolated_cwd` | An isolated working directory (restored on teardown). |
| `settings` | A fully-isolated `Settings` instance. |
| `sample_blueprint_dict` | A minimal valid blueprint dictionary. |
| `sample_blueprint_path` | The same blueprint, written to a temp directory. |
| `project_root` | An empty directory for a generated project. |
| `fake_forge_manifest` | A valid `.forge/manifest.json` in `project_root`. |
| `_isolate_environment` | An autouse fixture that clears `FORGE_*` env vars and redirects `HOME`. |

Use them by declaring them as parameters:

```python
@pytest.mark.unit()
def test_command_runs(cli_runner, forge_app) -> None:
    result = cli_runner.invoke(forge_app, ["ping"])
    assert result.exit_code == 0
```

### How to test an error path

Error paths are tested with `pytest.raises`:

```python
@pytest.mark.unit()
def test_invalid_input_raises_user_error() -> None:
    """Invalid input raises UserInputError, exits 1."""
    with pytest.raises(UserInputError) as exc_info:
        my_function(invalid_input="bad")

    assert exc_info.value.exit_code == ExitCode.USER_ERROR
    assert "invalid" in str(exc_info.value)
```

If the error is raised through the classifier (i.e., it propagates
to `main()`), the test asserts on `SystemExit`:

```python
@pytest.mark.unit()
def test_invalid_input_renders_friendly(
    cli_runner, forge_app
) -> None:
    """Invalid input renders a friendly message, exits 1."""
    result = cli_runner.invoke(forge_app, ["status", "--path", "/nonexistent"])
    assert result.exit_code == 1
    assert "not a directory" in result.output
```

---

## Common failures and their fixes

### "No module named 'X'"

The test imports a module that does not exist. Check the import
path, and confirm the module is at the expected location with
`find src -name "X.py"`.

### "Fixture 'X' not found"

The test uses a fixture that is not defined. Check `conftest.py` for
the fixture, and confirm the fixture name is spelled correctly. If
the fixture is local to a test class, it must be declared on the
class.

### "AttributeError: 'function' object has no attribute 'X'"

A module name and a function name are colliding. See
[`architecture/cli-architecture.md`](../architecture/cli-architecture.md#the-empty-initpy)
for the fix. This is the same class of bug that the empty
`__init__.py` prevents.

### "AssertionError: assert 'X' in ''"

The captured buffer was empty. The code under test wrote nothing.
Check that the code under test is being called (add a `print` in
the test), and that the right buffer is being captured (check the
fixture).

### "Required test coverage of 90% not reached"

See [Why not lower the gate?](#why-not-lower-the-gate) below.

---

## Why not lower the gate?

The 90% coverage gate is not a number chosen for fun. It is a
*contract*: every release ships with 90% of its code covered by
tests. That contract is what makes Forge trustworthy.

When the gate fails, there are two correct responses:

1. **Add tests** for the uncovered lines. This raises the coverage
   honestly.
2. **Exclude** the code that cannot be measured, with a comment in
   `pyproject.toml` explaining why (e.g., a three-line file whose
   behavior is tested by a subprocess test, which coverage cannot
   observe).

The *incorrect* response is to lower the gate. Lowering it says
"this release is less tested than we promised." The number is not
the point — the *commitment* is.

See [`architecture/overview.md`](../architecture/overview.md) for
the full rationale.

---

## Further reading

- [`../architecture/overview.md`](../architecture/overview.md) — the
  whole codebase
- [`../architecture/cli-architecture.md`](../architecture/cli-architecture.md)
  — the CLI layer in detail
- [`../architecture/error-handling.md`](../architecture/error-handling.md)
  — the classifier's contract
- [`adding-a-command.md`](adding-a-command.md) — how to add a command
  (which includes a test suite)
- [`adding-an-exception.md`](adding-an-exception.md) — how to add an
  exception (which includes a test suite)
- [`pyproject.toml`](../../pyproject.toml) — the pytest and coverage
  configuration
