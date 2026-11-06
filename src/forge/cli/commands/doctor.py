"""``forge doctor`` — diagnose environment and project health.

This command is the support tool. When a user reports "it does not
work," the first question is always "what does `forge doctor` say?"
It checks the environment (Python, uv, Node, Git, Docker), the
project (manifest validity, blueprint compatibility, required files),
and reports the results as a table (human mode) or JSON (``--json``
mode).

At the current stage it is a stub: it prints a TODO message. The
module's structure — the module-level ``app``, the explicit ``name=``
and ``help=`` on the ``Typer`` constructor, the ``@app.callback``
decorator with ``invoke_without_command=True``, the ``--json`` and
``--project-path`` options, the exit code contract — is final.
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
# in ``forge --help`` and ``forge doctor --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="doctor",
    help="Diagnose environment and project health.",
)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def doctor(
    project_path: Path = typer.Option(
        Path("."),
        "--project-path",
        "-p",
        help=(
            "Path to the project to diagnose. Defaults to the current "
            "directory. If the directory does not contain a "
            ".forge/manifest.json file, only environment checks are "
            "run and the project checks are reported as skipped."
        ),
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Output the full diagnostic report as JSON on stdout. "
            "Useful for bug reports and for piping into other tools."
        ),
    ),
) -> None:
    """Diagnose Forge installation, runtime, and project health.

    This is a stub. The real implementation will:

    1. Run environment checks:
       - Forge version (from ``forge.core.version.get_version``).
       - Python version (3.12+).
       - ``uv`` availability and version.
       - ``node`` availability and version (for frontend blueprints).
       - ``git`` availability and version.
       - ``docker`` availability and version (for Docker checks).
    2. Run project checks (if ``<project_path>/.forge/manifest.json``
       exists):
       - Manifest is valid (schema + semantic).
       - Blueprint referenced in the manifest is discoverable.
       - Blueprint version is compatible with the installed Forge.
       - Required files declared by the blueprint are present.
    3. For each failed check, provide a ``hint`` with the exact
       remediation command (e.g., "Install uv: `curl -LsSf
       https://astral.sh/uv/install.sh | sh`").
    4. Render the results as a Rich table on stdout (human mode) or
       as a JSON object on stdout (``--json`` mode).
    5. Exit with code 0 if all checks passed, 1 if any check
       produced a warning or failure.

    The distinction between "warning" and "failure" is intentional:

    - **Failure** means "the command cannot run without this" (e.g.,
      Python is not installed). The exit code is 1.
    - **Warning** means "the command can run, but something is
      suboptimal" (e.g., Docker is not installed, but this blueprint
      does not need Docker). The exit code is 1 (the user asked for
      diagnostics and received a non-clean bill of health), but the
      command does not crash.

    Exit code contract:

    - 0 — Every check passed. The environment and project are healthy.
    - 1 — At least one check produced a warning or failure.
    - 2 — User error (invalid ``--project-path``).
    - 5 — Internal error (unexpected exception).

    The ``--json`` flag is declared here, but its behaviour is a stub
    until the JSON output task lands. When the flag is passed, the
    stub will emit a JSON object with ``status: "not_implemented"``
    so scripts that depend on the flag can detect the stub
    programmatically. This is the same pattern as ``forge verify``.
    """
    # TODO: implement the diagnostic pipeline.
    # See the environment diagnostics and project validation tasks
    # for the real implementation.
    if json_output:
        # Emit a machine-readable marker so scripts can detect the
        # stub. The shape matches the planned diagnostic report
        # schema's outer envelope, so the eventual implementation can
        # replace this without changing the contract.
        import json

        typer.echo(
            json.dumps(
                {
                    "status": "not_implemented",
                    "data": {
                        "project_path": str(project_path),
                        "message": "forge doctor is not yet implemented",
                    },
                }
            )
        )
        return

    # Human-readable stub. Echo the resolved arguments so manual
    # verification of the CLI contract is trivial.
    typer.echo(f"TODO: doctor {project_path} (json={json_output})")
