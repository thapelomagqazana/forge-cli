"""forge list — List available blueprints."""
from __future__ import annotations

import typer

app = typer.Typer(help="List available blueprints.")


@app.callback(invoke_without_command=True)
def list_blueprints() -> None:
    """List all discoverable blueprints."""
    typer.echo("TODO: list blueprints")
