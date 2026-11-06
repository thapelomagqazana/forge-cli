"""Forge CLI application entry point."""
from __future__ import annotations

import typer
from rich.console import Console

from forge.cli.commands import create, doctor, list as list_cmd, validate, verify

app = typer.Typer(
    name="forge",
    help="The engineering foundation system for software projects.",
    no_args_is_help=True,
    add_completion=True,
)

app.add_typer(list_cmd.app, name="list")
app.add_typer(create.app, name="create")
app.add_typer(validate.app, name="validate")
app.add_typer(verify.app, name="verify")
app.add_typer(doctor.app, name="doctor")

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import PackageNotFoundError, version
        try:
            v = version("forge-cli")
        except PackageNotFoundError:
            v = "0.0.0.dev0"
        console.print(f"forge, version {v}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Forge CLI — Create, verify, and maintain engineering foundations."""


if __name__ == "__main__":
    app()
