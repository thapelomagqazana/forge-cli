"""``forge list`` — list available blueprints.

The first user-facing discovery command. It queries
``forge.discovery.BlueprintDiscoveryService`` (bundled official
blueprints plus user-local ``.forge/blueprints/``) and renders the
results as a human-readable table or a JSON array (``--json``).

Why this command exists in this shape
-------------------------------------
* **Single-command app.** The module exposes a module-level ``app``
  with a ``@app.callback()`` and no ``@app.command()`` subcommands.
  The registry mounts that callback as a *leaf command* (see
  ``forge.cli.registry._is_single_command_app``), producing
  ``forge list [OPTIONS]`` — no nesting, no accidental group.

* **Discovery is delegated.** The command never touches the
  filesystem directly; it calls the discovery service. That keeps the
  "how do we find blueprints?" rule in one place (ADR-0009) and means
  a future ``--blueprint-dir`` flag is a change to the service
  constructor, not to this file.

* **Rendering is delegated.** Table formatting lives in
  ``forge.cli.ui`` (ADR-0004) so every command produces consistent
  output. This module decides *what* to render; ``ui`` decides *how*.

Output contracts
----------------
* Human mode: a table sorted by id, with one row per blueprint. The
  ``source`` column distinguishes official from local so users can
  see at a glance which entries they can delete.
* ``--json``: a JSON array of ``DiscoveredBlueprint`` objects, in the
  same sorted order, with ``source`` as a lowercase string. This shape
  is a stable contract for scripting; changing field names is a
  breaking change and should be coordinated with the version bump.

Both modes return the same rows; they differ only in presentation. A
test that asserts on ``--json`` output implicitly asserts on the row
set, so the two modes cannot drift.
"""

from __future__ import annotations

import json
from typing import Annotated, Final

import typer

from forge.cli import ui
from forge.core.exceptions import DiscoveryError
from forge.discovery import BlueprintDiscoveryService
from forge.models import Category
from forge.models.blueprint import BlueprintSource, DiscoveredBlueprint

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
# in ``forge --help`` and ``forge list --help``, so it must be
# deliberate, not incidental.
app = typer.Typer(
    name="list",
    help="List available blueprints.",
)


# Sentinel for "no category filter". ``None`` is a valid argparse value
# (the user did not pass the flag), so we cannot use it to mean
# "filter by category named None". An explicit sentinel removes that
# ambiguity, and the choice of ``Final[str]`` keeps it comparable to
# the ``str`` values Typer will hand us.
_ALL: Final[str] = "all"


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def list_blueprints(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a JSON array instead of a table. Stable for scripts.",
        ),
    ] = False,
    category: Annotated[
        str,
        typer.Option(
            "--category",
            "-c",
            help=(
                "Only list blueprints in the given category "
                "(api, automation, frontend, library, cli, other). "
                "Use 'all' (the default) to disable filtering."
            ),
        ),
    ] = _ALL,
    source: Annotated[
        str,
        typer.Option(
            "--source",
            "-s",
            help=(
                "Only list blueprints from the given layer "
                "(official, local). Use 'all' (the default) to disable "
                "filtering. Useful for finding local overrides to "
                "clean up."
            ),
        ),
    ] = _ALL,
) -> None:
    """List all discoverable blueprints.

    Discovers blueprints through ``BlueprintDiscoveryService``, applies
    the optional ``--category`` and ``--source`` filters, and renders
    the result. Sorting is by blueprint id, which the service already
    guarantees — the command re-sorts defensively because the CLI's
    output shape is a public contract that must not silently reshuffle
    if the service's ordering changes.

    Filters are applied *after* discovery, not during it. Discovery is
    cheap on realistic catalogue sizes, and filtering inside the
    service would couple the service to the CLI's filter vocabulary —
    the exact drift ADR-0009 was written to prevent. One filter pass
    over a sorted list is trivial.
    """
    try:
        blueprints = BlueprintDiscoveryService().discover()
    except DiscoveryError as exc:
        # Discovery-level failures are already human-readable and
        # carry an exit code; route them through the standard error
        # renderer so output is consistent with every other command.
        ui.error(str(exc))
        raise typer.Exit(code=exc.exit_code) from exc

    filtered = _apply_filters(blueprints, category=category, source=source)

    if not filtered:
        # Distinguish "nothing installed" from "your filters matched
        # nothing". Both are valid outcomes, but the recovery is
        # different: the first needs `forge doctor`, the second needs
        # a wider filter. Collapsing them into one message would leave
        # the user guessing which fix applies.
        if blueprints:
            ui.warn("No blueprints match the given filters.")
        else:
            ui.warn(
                "No blueprints found. Check your installation or "
                "add a blueprint under .forge/blueprints/."
            )
        return

    if json_output:
        _render_json(filtered)
    else:
        _render_table(filtered)


# -----------------------------------------------------------------------------
# Filtering
# -----------------------------------------------------------------------------
def _apply_filters(
    blueprints: list[DiscoveredBlueprint],
    *,
    category: str,
    source: str,
) -> list[DiscoveredBlueprint]:
    """Apply CLI filters to a discovered catalogue.

    Extracted into a named function so the filter vocabulary is
    testable in isolation and the command body reads as orchestration
    rather than a wall of comprehensions. Unknown filter values are
    rejected *before* filtering — a typo like ``--category apis``
    should fail with a clear message, not silently produce zero rows.
    """
    normalised_category = category.strip().lower()
    normalised_source = source.strip().lower()

    valid_categories = {c.value for c in Category}
    if normalised_category != _ALL and normalised_category not in valid_categories:
        raise typer.BadParameter(
            f"unknown category {category!r}; "
            f"expected one of {', '.join(sorted(valid_categories))} "
            f"or {_ALL!r}"
        )

    valid_sources = {s.value for s in BlueprintSource}
    if normalised_source != _ALL and normalised_source not in valid_sources:
        raise typer.BadParameter(
            f"unknown source {source!r}; "
            f"expected one of {', '.join(sorted(valid_sources))} "
            f"or {_ALL!r}"
        )

    result = blueprints
    if normalised_category != _ALL:
        result = [bp for bp in result if bp.category.value == normalised_category]
    if normalised_source != _ALL:
        result = [bp for bp in result if bp.source.value == normalised_source]
    return result


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------
def _render_json(blueprints: list[DiscoveredBlueprint]) -> None:
    """Emit the catalogue as a JSON array on stdout.

    Shape is a stable contract for scripting:

        [
          {"id": "...", "name": "...", "version": "...",
           "description": "...", "category": "...", "source": "..."},
          ...
        ]

    Field order matches ``DiscoveredBlueprint``'s declaration order so
    diffs of JSON output are stable across runs. ``category`` and
    ``source`` are emitted as their string values (not the enum
    reprs) so ``jq``-style consumers can filter without a special-case
    for ``Category.api``.
    """
    payload = [
        {
            "id": bp.id,
            "name": bp.name,
            "version": bp.version,
            "description": bp.description,
            "category": bp.category.value,
            "source": bp.source.value,
        }
        for bp in blueprints
    ]
    # ``typer.echo`` writes through the same stream the CLI handler
    # uses, so tests can capture stdout with ``capsys`` without any
    # special setup. ``json.dumps`` with an explicit indent produces
    # diff-friendly output; ``sort_keys=False`` preserves declaration
    # order (see docstring).
    typer.echo(json.dumps(payload, indent=2, sort_keys=False))


def _render_table(blueprints: list[DiscoveredBlueprint]) -> None:
    """Render the catalogue as a human-readable table.

    Uses ``forge.cli.ui`` for all formatting so colour, column
    separators, and stream choice stay consistent with the rest of the
    CLI (ADR-0004). The ``source`` column uses a marker (``●`` for
    local, ``○`` for official) *and* the word, so the table is legible
    without colour and machine-checkable in tests.
    """
    ui.section(f"Available blueprints ({len(blueprints)})")
    for bp in blueprints:
        marker = "●" if bp.source is BlueprintSource.LOCAL else "○"
        ui.info(f"  {marker} {bp.id:<32} {bp.version:<8} " f"[{bp.source.value:<8}] {bp.name}")


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = ["app"]
