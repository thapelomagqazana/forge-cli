"""forge verify — Verify a generated project."""

from __future__ import annotations

import typer

app = typer.Typer(help="Verify a generated project.")


@app.command()
def verify(
    check: str | None = typer.Option(None, "--check", help="Run only this check ID."),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
) -> None:
    """Run the project's verification baseline."""
    typer.echo("TODO: verify")
