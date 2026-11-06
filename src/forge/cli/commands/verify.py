"""``forge verify`` — verify a generated project against its baseline.

This command is the verification entry point. It reads the project's
manifest (``.forge/manifest.json``), loads the blueprint that generated
the project, resolves the blueprint's verification contract, executes
every declared check (or a single check if ``--check`` was passed), and
renders a summary.

The distinction between "generated" and "verified" is the whole point
of this command: a project is "generated" if the files exist, and
"verified" only if every required check passes. ``forge verify`` is
what turns the first into the second.

At the current stage it is a stub: it prints a TODO message. The
module's structure — the module-level ``app``, the explicit ``name=``
and ``help=`` on the ``Typer`` constructor, the ``@app.command``
decorator, the ``--check`` and ``--json`` options, the exit code
contract — is final.
"""

from __future__ import annotations

from pathlib import Path

import typer

# -----------------------------------------------------------------------------
# Command module contract
# -----------------------------------------------------------------------------
# Every command module exposes a module-level ``app`` attribute of type
# ``typer.Typer``. The registry (``forge.cli.registry``) discovers this
# attribute and mounts the app on the root CLI under ``name``.
#
# The explicit ``name=`` and ``help=`` are not strictly required (Typer
# falls back to the module name and an empty help string if omitted),
# but they are strongly preferred: the help text is what the user sees
# in ``forge --help`` and ``forge verify --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="verify",
    help="Verify a generated project against its baseline checks.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.command()
def verify(
    project_path: Path = typer.Option(
        Path("."),
        "--project-path",
        "-p",
        help=(
            "Path to the project to verify. Defaults to the current "
            "directory. The directory must contain a .forge/manifest.json "
            "file."
        ),
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
    ),
    check: str | None = typer.Option(
        None,
        "--check",
        "-c",
        help=(
            "Run only the named check (by its stable ID, e.g., 'tests', "
            "'lint', 'typecheck'). If omitted, run every check declared "
            "in the blueprint's verification contract."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Output the full verification result as JSON on stdout. "
            "Useful for CI and for piping into other tools."
        ),
    ),
) -> None:
    """Run the project's verification baseline.

    This is a stub. The real implementation will:

    1. Load the project's manifest from ``<project_path>/.forge/manifest.json``.
       Fail with exit code 2 (``ExitCode.BLUEPRINT_ERROR``) if the
       manifest is missing, unreadable, or invalid.
    2. Load the blueprint referenced in the manifest. Fail with exit
       code 2 if the blueprint is not found, or if its version is
       incompatible with the installed Forge.
    3. Resolve the blueprint's verification contract into a list of
       checks. If ``--check`` was passed, filter to the named check
       (fail with exit code 1 if the check ID is unknown).
    4. Execute each check using the check registry. Each check
       produces a ``CheckResult`` with ``status``, ``duration``, and
       ``diagnostic``.
    5. Aggregate the results into a ``VerificationResult``. The
       overall status is ``PASSED`` only if every required check
       passed; ``FAILED`` if any required check failed or errored;
       ``SKIPPED`` if every check was skipped; ``NOT_RUN`` if there
       were no checks.
    6. Render the result as a Rich table on stdout (human mode) or
       as a JSON object on stdout (``--json`` mode).
    7. Exit with code 0 if the overall status is ``PASSED``, 4
       (``ExitCode.VERIFICATION_FAILED``) if ``FAILED``, or 5
       (``ExitCode.INTERNAL_ERROR``) on unexpected errors.

    Exit code contract:

    - 0 — All required checks passed (or ``--check`` named a check
      that passed).
    - 1 — User error (unknown ``--check`` ID, invalid path).
    - 2 — Blueprint error (manifest missing or invalid, blueprint
      not found, incompatible version).
    - 4 — Verification failed (one or more required checks failed
      or errored).
    - 5 — Internal error (unexpected exception).

    The ``--json`` flag is declared here, but its behaviour is a stub
    until the JSON output task lands. When the flag is passed, the
    stub will emit a JSON object with ``status: "not_implemented"``
    so scripts that depend on the flag can detect the stub
    programmatically. This is intentional: a stub that *claims* to
    support ``--json`` but silently emits human output is worse than
    a stub that tells the truth.
    """
    # TODO: implement the verification pipeline.
    # See the verification engine, check registry, and check handler
    # tasks for the real implementation.
    if json_output:
        # Emit a machine-readable marker so scripts can detect the
        # stub. The shape matches the planned VerificationResult
        # schema's outer envelope (``status`` + ``data``), so the
        # eventual implementation can replace this without changing
        # the contract.
        import json

        typer.echo(
            json.dumps(
                {
                    "status": "not_implemented",
                    "data": {
                        "project_path": str(project_path),
                        "check": check,
                        "message": "forge verify is not yet implemented",
                    },
                }
            )
        )
        return

    # Human-readable stub. Echo the resolved arguments so manual
    # verification of the CLI contract is trivial.
    typer.echo(f"TODO: verify {project_path} " f"(check={check!r}, json={json_output})")
