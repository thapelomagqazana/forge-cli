"""Forge CLI — the command-line interface package.

This package exposes the public entry points of the CLI. External code
should import from ``forge.cli``, not from ``forge.cli.main``, to keep
the public surface stable across refactors.
"""

from __future__ import annotations

from forge.cli.main import create_app, main

__all__ = ["create_app", "main"]
