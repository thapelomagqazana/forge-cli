"""forge create — Create a project from a blueprint."""

from __future__ import annotations

import typer

app = typer.Typer(help="Create a project from a blueprint.")


@app.command()
def create(
    blueprint: str = typer.Argument(..., help="Blueprint ID (e.g., python.fastapi.api)"),
    name: str = typer.Option(..., "--name", "-n", help="Project name."),
    output: str = typer.Option(".", "--output", "-o", help="Destination directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing."),
) -> None:
    """Create a new project from a blueprint."""
    typer.echo(f"TODO: create {blueprint} as {name} in {output}")
