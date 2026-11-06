"""forge validate — Validate a blueprint."""
from __future__ import annotations

import typer

app = typer.Typer(help="Validate a blueprint definition.")


@app.command()
def validate(
    path: str = typer.Argument(..., help="Path to blueprint.yaml or directory."),
) -> None:
    """Validate a blueprint without generating a project."""
    typer.echo(f"TODO: validate {path}")
