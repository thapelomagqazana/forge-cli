"""forge doctor — Diagnose environment and project health."""
from __future__ import annotations

import typer

app = typer.Typer(help="Diagnose environment and project health.")


@app.callback(invoke_without_command=True)
def doctor() -> None:
    """Diagnose Forge installation, runtime, and project health."""
    typer.echo("TODO: doctor")
