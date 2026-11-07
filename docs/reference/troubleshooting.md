# Troubleshooting

Something went wrong with Forge CLI. This page helps you diagnose
and fix it.

The symptoms are organized by what you *saw*, not by what the
underlying problem is. Find the section that matches your
experience, and follow the steps.

If your symptom is not listed here, run `forge doctor` — it prints
a diagnostic report that you can attach to a bug report at
[github.com/thapelomagqazana/forge-cli/issues](https://github.com/thapelomagqazana/forge-cli/issues).

---

## Quick checks

Before anything else, run these three commands. They answer the
most common "is it installed correctly?" questions:

```bash
forge --version      # prints the installed version
forge --help         # prints the command list
forge doctor         # prints a diagnostic report
```

- If `forge --version` fails, skip to
  [Forge is not installed](#forge-is-not-installed).
- If `forge --help` fails, skip to
  [The CLI crashes on every command](#the-cli-crashes-on-every-command).
- If `forge doctor` reports failures, follow the hints it prints.
  The most common ones are covered below.

---

## Installation

### Forge is not installed

**Symptom:** `forge --version` prints `forge: command not found` (or
the Windows equivalent, `'forge' is not recognized as an internal or
external command`).

**Cause:** The `forge` executable is not on your `PATH`.

**Fix:** Depends on how you installed it.

**If you installed with `uv tool install`:**

The executable is at `~/.local/bin/forge` (Linux/macOS) or
`%USERPROFILE%\.local\bin\forge.exe` (Windows). Add that directory
to your `PATH`:

```bash
# Linux / macOS — add to ~/.bashrc or ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
```

```powershell
# Windows — add to your user PATH (persists across sessions)
[Environment]::SetEnvironmentVariable(
    "Path",
    [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:USERPROFILE\.local\bin",
    "User"
)
```

Open a new terminal and run `forge --version` again.

**If you installed with `pipx`:**

The executable is at `~/.local/bin/forge`. Follow the same steps as
above.

**If you installed with `pip`:**

The executable is in your virtual environment's `bin/` directory.
Activate the venv first:

```bash
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
forge --version
```

If you installed with `pip` into a system Python (not recommended),
the executable may be at `/usr/local/bin/forge` or
`~/.local/bin/forge` depending on your platform.

### "No module named 'forge'"

**Symptom:** `forge --version` prints `ModuleNotFoundError: No module
named 'forge'`.

**Cause:** The `forge` executable exists, but the `forge` Python
package it imports does not. This usually means the package was
partially uninstalled, or the executable is from an older install.

**Fix:** Reinstall:

```bash
uv tool install --reinstall forge-cli
```

Or, if you are using `pipx`:

```bash
pipx reinstall forge-cli
```

### "No module named 'click'" (or another dependency)

**Symptom:** Forge crashes on startup with `ModuleNotFoundError: No
module named 'X'`.

**Cause:** A dependency is missing from the installed environment.
This usually means the installation was interrupted, or a system
package manager removed a dependency.

**Fix:** Reinstall:

```bash
uv tool install --reinstall forge-cli
```

If the problem persists, run `forge --version` to confirm the
version, then run `uv tool list` to see what `uv` thinks is
installed. The two should match.

---

## Running commands

### `forge: command not found` after upgrading

**Symptom:** You upgraded Forge, and now `forge` is not found.

**Cause:** The upgrade re-created the executable, but the shell has
not picked up the new `PATH`. Or the old executable was removed and
the new one is in a different location.

**Fix:**

1. Open a new terminal.
2. Run `forge --version`.
3. If it still fails, run `which forge` (or `where forge` on
   Windows) to find the executable, and add its directory to your
   `PATH`.

### `forge create` says the target directory already exists

**Symptom:**

```
✗ target directory ./my-api already exists.
Hint: choose a different --name, or pass --overwrite to replace it.
```

**Cause:** Forge refuses to overwrite an existing directory by
default. This is a safety feature — Forge never silently overwrites
user files.

**Fix:** Choose one of:

1. **Use a different name:** `forge create python.fastapi.api --name my-api-2`.
2. **Use a different output:** `forge create python.fastapi.api --name my-api --output ~/projects/`.
3. **Delete the existing directory** (if you are certain you do not
   need it): `rm -rf ./my-api`.
4. **Pass `--overwrite`** (if you are certain the existing content
   can be replaced): `forge create python.fastapi.api --name my-api --overwrite`.

Forge backs up overwritten files before replacing them, so
`--overwrite` is recoverable — but it is not recommended unless you
have reviewed what is being replaced.

### `forge create` says the project name is invalid

**Symptom:**

```
✗ project name 'My API' is invalid.
Hint: use a lowercase, hyphenated name (e.g., 'payments-api').
```

**Cause:** The project name must be a valid Python package name. It
must be lowercase, use only letters, digits, and hyphens, and start
with a letter.

**Fix:** Use a valid name. For example:

- ❌ `My API` (spaces, uppercase)
- ❌ `123-api` (starts with a digit)
- ❌ `my_api` (underscore — use `my-api` instead)
- ✅ `my-api`
- ✅ `payments-api`
- ✅ `customer-portal`

---

## Verification

### `forge verify` says a check is `SKIPPED`

**Symptom:**

```
  - docker                SKIPPED  (Docker not available)
```

**Cause:** A check that is declared *optional* by the blueprint is
not available. The most common case is the Docker check, which
requires Docker to be installed. A skipped check does *not* fail
verification — the overall result can still be `PASSED`.

**Fix:** If you want the check to run, install Docker. If you do not
need it, you can ignore the skip — it does not affect the exit code.

**Which checks are optional?** It depends on the blueprint. The
`python.fastapi.api` blueprint makes `docker` optional. If you are
unsure, run `forge verify --check <id>` for the specific check, or
read the blueprint's documentation.

### `forge verify` says a check is `FAILED`

**Symptom:**

```
  ✗ tests                 FAILED   2.14s
     2 tests failed. Run `pytest -v` for details.
```

**Cause:** A required check did not pass. The message tells you
which check and gives a hint for how to investigate.

**Fix:** Depends on the check:

- **`tests` failed:** Run `pytest -v` in the project to see which
  tests failed, then fix them.
- **`lint` failed:** Run `ruff check` to see the lint errors, then
  fix them (or run `ruff check --fix` to auto-fix).
- **`typecheck` failed:** Run `mypy src` to see the type errors, then
  fix them.
- **`config` failed:** The project's configuration is invalid. Check
  `.env`, `.forge/manifest.json`, and any config files.
- **`required-files` failed:** A file the blueprint declared as
  required is missing. Check the blueprint's documentation for the
  full list.
- **`ci-config` failed:** The CI workflow is invalid or missing.
  Check `.github/workflows/`.

After fixing the issue, re-run `forge verify`.

### `forge verify` says "verification failed" but every check passed

**Symptom:** The summary says `Overall: FAILED` but the table shows
every check as `PASSED`.

**Cause:** This is a bug. The overall status is `PASSED` only when
every *required* check passes. If the summary disagrees with the
table, something is wrong with the aggregation logic.

**Fix:** Report a bug with the full output of `forge verify --json`
(the JSON contains the aggregation fields), and the output of
`forge doctor`.

### `forge verify` says "not_implemented"

**Symptom:**

```
$ forge verify --json
{"status": "not_implemented", "data": {...}}
```

**Cause:** `forge verify` is a stub in this version of Forge. The
verification engine has not yet landed.

**Fix:** Wait for a future release. In the meantime, you can run the
individual checks manually (e.g., `pytest`, `ruff check`, `mypy`).

---

## Diagnostics

### `forge doctor` says Docker is not available

**Symptom:**

```
✗ docker  Docker is not available
```

**Cause:** Docker is not installed, the Docker daemon is not
running, or the `docker` executable is not on your `PATH`.

**Fix:**

1. **Check if Docker is installed:** `docker --version`. If it
   prints a version, skip to step 2.
2. **Check if the Docker daemon is running:** `docker info`. If it
   fails, start Docker (or restart it).
3. **Check your `PATH`:** `which docker` (or `where docker` on
   Windows). If it fails, add Docker's `bin/` directory to your
   `PATH`.

Docker is an *optional* dependency for most Forge commands. If you
do not need it, you can ignore the doctor warning.

### `forge doctor` says "manifest is invalid"

**Symptom:**

```
✗ manifest  .forge/manifest.json is invalid: missing field 'blueprint'
```

**Cause:** The project's `.forge/manifest.json` is malformed. This
usually means the project was created by an older (incompatible)
version of Forge, or the manifest was manually edited.

**Fix:**

1. **If the project was created by an older Forge:** upgrade Forge.
   The manifest migration lands in a future release.
2. **If the manifest was manually edited:** restore it from a backup
   or from version control.
3. **If the manifest is missing entirely:** run `forge init` (future
   — not yet implemented) to re-create it.

Until the migration lands, the workaround is to delete
`.forge/manifest.json` and let the next command regenerate it. Note
that this loses the project's provenance (which blueprint version
created it), so `forge verify` may not be able to determine the
baseline.

---

## Errors and crashes

### The CLI crashes on every command

**Symptom:** Every command fails with a traceback, or with
`✗ An unexpected internal error occurred.`

**Cause:** Something is broken in Forge itself. This is not a user
error.

**Fix:**

1. **Run with `--debug`** to see the full traceback:
   ```bash
   forge --debug <command>
   ```
2. **Report a bug** with the output of `forge --debug` and the
   output of `forge doctor`. The traceback tells the maintainer
   which module is broken.
3. **Reinstall Forge** as a temporary workaround:
   ```bash
   uv tool install --reinstall forge-cli
   ```

### `forge --debug <command>` shows a traceback I do not understand

**Symptom:** You ran `forge --debug` and got a long Python traceback.
You do not know how to interpret it.

**Cause:** `--debug` is a tool for developers. The traceback is
useful to a maintainer, not to a user.

**Fix:** Do not try to interpret it. Copy the full output (including
the traceback) and paste it into a bug report at
[github.com/thapelomagqazana/forge-cli/issues](https://github.com/thapelomagqazana/forge-cli/issues).
The maintainer will read the traceback and determine what went
wrong.

If you want a *friendly* error message instead of the traceback,
re-run the command without `--debug`.

### Ctrl+C does not cancel the command

**Symptom:** You pressed Ctrl+C, but the command kept running.

**Cause:** Some operations (e.g., waiting on a network response,
reading a large file) may not check for cancellation frequently
enough. This is a bug, but it is usually a transient one — press
Ctrl+C again.

**Fix:**

1. **Press Ctrl+C again.** The first Ctrl+C may have been consumed
   by a subprocess that did not terminate cleanly. A second press
   usually works.
2. **If the command still does not stop:** close the terminal. The
   command will be terminated by the shell. Note that any
   in-progress file operations may be left in an inconsistent
   state — Forge will detect and recover them on the next run.
3. **If the command was a `forge create` that was cancelled:**
   check the target directory. If it exists but is empty, delete
   it. If it exists and has files, check the files before
   proceeding — Forge's staging mechanism should have prevented
   partial output, but a cancelled commit may have left a partial
   state.

---

## Configuration

### `forge` ignores my `.env` file

**Symptom:** You set an environment variable in `.env`, but Forge
does not see it.

**Cause:** Forge loads `.env` only from the *current working
directory* (and only when `FORGE_` prefixed, depending on the
setting). It does not search parent directories.

**Fix:**

1. **Confirm the working directory:** run `forge <command>` from
   the directory that contains `.env`.
2. **Confirm the variable name:** Forge's configuration variables
   are prefixed with `FORGE_` (e.g., `FORGE_LOG_LEVEL`). A variable
   without the prefix is not loaded.
3. **Confirm the file is parsed:** run `forge doctor` — it reports
   which configuration files were loaded.

### `forge` ignores my global config file

**Symptom:** You set a default in `~/.config/forge/config.yaml`, but
the command does not use it.

**Cause:** Precedence. Forge reads configuration in this order
(highest priority last):

1. Built-in defaults.
2. `~/.config/forge/config.yaml`.
3. `.forge/config.yaml` (project).
4. `FORGE_*` environment variables.
5. Command-line flags.

A higher-priority source is overriding your global config.

**Fix:** Check which source is overriding. Run `forge doctor` —
it reports the effective configuration and where each value came
from.

---

## Permissions

### "Permission denied" when writing to the target directory

**Symptom:**

```
✗ permission denied: cannot write to /path/to/output.
Hint: choose a writable --output directory, or check permissions.
```

**Cause:** Your user does not have write permission on the target
directory.

**Fix:**

1. **Check the directory's permissions:** `ls -ld /path/to/output`.
2. **Choose a writable output:** `forge create python.fastapi.api --name my-api --output ~/projects/`.
3. **Fix the permissions** (if you have `sudo`): `sudo chown -R $USER /path/to/output`. Be careful — changing permissions on system directories can cause other problems.

### "Permission denied" when installing Forge

**Symptom:** `uv tool install forge-cli` fails with a permission
error.

**Cause:** `uv` is trying to install into a directory you do not
own. This is common with system-wide installs.

**Fix:** `uv tool install` installs into `~/.local/bin` by default,
which is user-owned. If the permission error mentions a different
directory, your `uv` configuration is overriding the default.
Check `uv config get install-dir` and either fix the config or use
`uv tool install --install-dir ~/.local/bin forge-cli`.

---

## Getting help

If your symptom is not covered here:

1. **Run `forge doctor --json`** and save the output. It contains
   the environment diagnostics that a maintainer will ask for.
2. **Search existing issues** at
   [github.com/thapelomagqazana/forge-cli/issues](https://github.com/thapelomagqazana/forge-cli/issues).
   Someone may have already reported the problem.
3. **Open a new issue** with:
   - The command you ran.
   - The output (human or `--json`).
   - The output of `forge doctor --json`.
   - The output of `forge --version`.
   - Your OS and Python version (`uname -a` or `systeminfo` and
     `python --version`).

The maintainer will respond within two business days.

---

## Further reading

- [`reference/exit-codes.md`](reference/exit-codes.md) — what each
  exit code means
- [`reference/streams.md`](reference/streams.md) — where error
  messages go
- [`reference/commands/`](reference/commands/) — per-command
  reference
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — if you want to fix
  the bug yourself
- [`SECURITY.md`](../SECURITY.md) — if you are reporting a security
  vulnerability
