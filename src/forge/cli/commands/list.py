"""``forge list`` — list available blueprints.

The first user-facing discovery command. It queries
``forge.discovery.BlueprintDiscoveryService`` (bundled official
blueprints plus user-local ``.forge/blueprints/``) and renders the
results as a human-readable table or a JSON envelope (``--json``).

Why this command exists in this shape
-------------------------------------
* **Thin command, testable seam.** The command body lives in
  ``run(args) -> int`` — a plain function that returns an exit code and
  never touches a console, ``sys.exit``, or the Typer machinery. A
  two-line Typer adapter (``app``) builds an argument namespace and
  delegates to ``run``. Tests drive ``run`` directly (no
  ``CliRunner``, no ``sys.argv`` patching); users go through the
  adapter, which does nothing but translate. The two paths share one
  code path, so they cannot drift.

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

* **Rendering is delegated.** Table and JSON rendering live in
  ``forge.cli.renderers`` (ADR-0012). This module decides *what* to
  render and *in what order*; the renderers decide *how*. Sorting
  happens exactly once, here, before the DTO is built — the renderers
  preserve order verbatim.

* **Sorting is the command's job.** ``CLI_LIST_OUTPUT.md`` promises
  ``blueprints`` is sorted by ``id`` ascending, and consumers may rely
  on it for the current ``schema_version``. That promise is honoured
  here, by a single ``sorted(..., key=lambda pair: pair[0].id)``
  between filter and DTO projection. Putting the sort in the renderer
  would let the two renderers disagree; putting it in the discovery
  service would let the CLI's output contract depend on an internal
  implementation detail of discovery. See "Where sorting lives" below.

* **Usage errors are the adapter's job.** Typer has two error paths,
  and only one of them is guaranteed to produce the standard
  usage-error presentation. An exception raised *during argument
  parsing* (from an option's ``callback=``, or by Click while reading
  ``sys.argv``) is caught and rendered by Typer; an exception raised
  *from inside the callback body* is not caught by that machinery.
  Because ``_validate_filters`` runs inside ``run`` (which is inside
  the callback body), its ``ValueError`` cannot rely on Typer to
  classify it. The adapter renders the message explicitly and raises
  ``typer.Exit(code=2)`` — same observable behaviour, no dependence
  on which of Typer's two paths happens to catch the raise.

Output contracts
----------------
* Human mode: the table from ``render_table``. Coloured framing (if
  any) is applied by ``ui.emit``, never by the renderer.
* ``--json``: the envelope from ``render_json`` — a versioned
  ``ListOutput`` payload with a ``schema_version`` field. Changing
  that shape is a breaking change and requires bumping the DTO's
  ``schema_version`` (see ``CLI_LIST_OUTPUT.md``).

Both modes consume the *same* ``ListOutput`` DTO, so a test that
asserts on ``--json`` output implicitly asserts on the row set the
table mode will produce, and vice versa.

Where ``BlueprintStatus`` comes from
------------------------------------
It is imported from ``forge.models.status`` — its home module. Earlier
drafts of this file imported it from ``forge.models.blueprint``; that
was wrong, because ``blueprint.py`` is generated from the canonical
JSON Schema (ADR-0008) and hand-adding an enum to it is erased on the
next ``make models`` run. ``forge.models.status`` is a small,
hand-written module that exists specifically so that discovery (which
returns the enum) can import it without depending on the CLI (ADR-0009),
and so that the CLI (which projects it into a DTO) can re-export it
without creating a second definition. There is exactly one
``BlueprintStatus`` class in the codebase; three import paths resolve
to it. A guard test
(``tests/unit/models/test_status_single_definition.py``) asserts the
identity invariant.

Where sorting lives
-------------------
``forge list`` sorts its output by ``id`` ascending, and it does so in
exactly one place: ``run()``, immediately before the DTO is built.

The three candidate locations and why only this one is right:

* **Not in the discovery service.** The service is an implementation
  detail of *how we find blueprints*. It happens to return a sorted
  catalogue today (see ``resolver.to_metadata``), but that is a
  convenience, not a contract. If discovery someday returns blueprints
  in a different order (e.g. because a remote source returns results
  in arrival order), the CLI's output would silently reshuffle, and
  every consumer relying on ``CLI_LIST_OUTPUT.md`` would break. The
  CLI's ordering is the *CLI's* contract; it must not be inherited
  from a lower layer that does not know about it.

* **Not in the renderer.** Renderers are pure projections from DTO to
  string. If the table renderer sorted and the JSON renderer did not
  (or vice versa), the two outputs would disagree — the exact bug
  that ``test_list_stable.py::TestDeterminism::test_table_and_json_agree_on_order``
  exists to catch. Sorting must happen *before* the DTO is built, so
  both renderers see the same order.

* **Here, in ``run()``.** Between filter and DTO projection. That is
  the last point where the command still has the full pair list, so
  the status travels with the blueprint through the sort, and the
  first point where the ordering is a promise to the outside world.

Why this file is ASCII-only
---------------------------
The module's job is to be trivially readable and correct. Em dashes,
curly quotes, and other non-ASCII punctuation do not add meaning
here, and they are a recurring source of corruption when a file is
opened, saved, or copied between tools with different default
encodings. Plain ASCII on this file means every tool, on every
platform, in every locale, reads it exactly as written. Use ``--``
for an em dash and ``'`` for a curly apostrophe; leave the pretty
typography to prose that is not code.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Final

import typer

from forge.cli import ui
from forge.cli.renderers import render_json, render_table
from forge.cli.schemas.list_output import ListOutput, ListOutputItem
from forge.core.exceptions import DiscoveryError
from forge.discovery import BlueprintDiscoveryService
from forge.models import Category
from forge.models.blueprint import BlueprintSource, DiscoveredBlueprint
from forge.models.status import BlueprintStatus

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


# Sentinel for "no filter applied". ``None`` is a valid argparse value
# (the user did not pass the flag), so we cannot use it to mean "filter
# by a category literally named None". An explicit sentinel removes
# that ambiguity, and the choice of ``Final[str]`` keeps it comparable
# to the ``str`` values Typer will hand us.
_ALL: Final[str] = "all"


# -----------------------------------------------------------------------------
# Testable core
# -----------------------------------------------------------------------------
def run(args: SimpleNamespace) -> int:
    """Execute ``forge list`` with the given arguments. Return the exit code.

    This is the seam the Typer adapter calls and the seam tests drive.
    Keeping it as a plain function means:

    * tests never touch Typer's ``CliRunner``, ``sys.argv``, or the
      ``standalone_mode`` dance;
    * the exit-code contract is a *return value*, not a raised
      exception, so a test can assert ``run(...) == 0`` directly;
    * any future non-CLI entry point (an HTTP catalog endpoint, an
      editor integration) can call the same code path.

    The command reads exactly three attributes off ``args``:
    ``json``, ``category``, and ``source``. Any object providing them
    works; the Typer adapter below passes a ``SimpleNamespace``. This
    is deliberate — the seam is duck-typed so a future test does not
    need to instantiate an argparse namespace to drive the command.

    Filters are validated **before** discovery, not after. A typo like
    ``--category apis`` should fail with a clear message regardless of
    whether any blueprints are installed; discovering first and then
    finding that the filter was invalid would produce a confusing "no
    blueprints match" message for an argument error.

    Exceptions vs. return codes
    ---------------------------
    ``run`` returns an ``int`` for *user-facing* outcomes (success,
    empty catalogue) and raises for *programmer-facing* outcomes
    (invalid filter values passed programmatically — a bug in the
    caller, not in the input). The Typer adapter catches the latter,
    renders it via ``ui.error``, and raises ``typer.Exit(code=2)``,
    which produces the standard usage-error presentation and exit
    code. That translation belongs in the adapter, not here, because
    it depends on the Typer framework and ``run`` must be usable
    outside it.
    """
    category = getattr(args, "category", _ALL)
    source = getattr(args, "source", _ALL)
    json_mode = getattr(args, "json", False)

    # Validate filters up front so a typo is a *usage* error (exit 2),
    # not a silently-empty result. See `_validate_filters` for the
    # exact reason this happens before discovery.
    _validate_filters(category=category, source=source)

    try:
        blueprints = BlueprintDiscoveryService().discover_with_status()
    except DiscoveryError as exc:
        # Discovery-level failures are domain errors; the CLI should
        # render them uniformly (ADR-0004) rather than leak a traceback.
        # `DiscoveryError` carries its own `exit_code`. Any other
        # exception is an internal error and is deliberately re-raised
        # so `forge.cli.errors.handle_exception` classifies it as exit
        # code 5 — a bare `except Exception` here would hide bugs.
        ui.error(str(exc))
        return exc.exit_code

    filtered = _apply_filters(blueprints, category=category, source=source)

    if not filtered:
        # The recovery differs between "nothing installed" and "your
        # filters matched nothing", so the *table* path says which one
        # applies. The JSON path does not: machine consumers asked for
        # a payload, not a diagnostic, and the empty envelope *is* the
        # answer. Emitting the envelope here — rather than returning
        # early and letting stdout be empty — is what makes
        # ``forge list --json | jq '.blueprints | length'`` return
        # ``0`` instead of crashing on empty input.
        if json_mode:
            ui.emit(render_json(ListOutput(blueprints=[])))
            return 0

        if blueprints:
            ui.warning("No blueprints match the given filters.")
        else:
            ui.warning(
                "No blueprints found. Check your installation or "
                "add a blueprint under .forge/blueprints/."
            )
        # Empty is a valid answer, not a failure — exit 0 (ADR-0012).
        return 0

    # Sort once, here, before building the DTO. Both renderers receive
    # this exact ordering. Per ADR-0012, sorting belongs to the command,
    # not to the renderers (which preserve input order verbatim), and
    # not to the discovery service (whose internal ordering is an
    # implementation detail that the CLI's output contract must not
    # silently depend on).
    #
    # ``sorted(...)`` rather than ``.sort()``: ``_apply_filters``
    # returns its input *unchanged* when no filters are applied, so an
    # in-place sort would mutate the caller's list. That is a latent
    # bug today (nobody observes the mutated list) and a live one the
    # moment a future caller does. ``sorted()`` returns a new list and
    # cannot have that failure mode.
    #
    # The sort key is ``pair[0].id``, not ``pair[0]``. Pydantic v2
    # models do not implement ``__lt__`` by default, so sorting the
    # pair objects directly would raise ``TypeError``. The explicit
    # key also documents what the ordering contract actually is.
    filtered = sorted(filtered, key=lambda pair: pair[0].id)

    # Project into the versioned DTO exactly once, then hand the *same*
    # DTO to whichever renderer the user selected. This is what makes
    # the table and the JSON agree on field names, field order, and
    # values without duplicated projection logic — the whole point of
    # ADR-0012.
    output = ListOutput(
        blueprints=[
            ListOutputItem(
                id=bp.id,
                name=bp.name,
                version=bp.version,
                category=bp.category,
                status=status,
            )
            for bp, status in filtered
        ]
    )

    if json_mode:
        ui.emit(render_json(output))
    else:
        ui.emit(render_table(output.blueprints))
    return 0


# -----------------------------------------------------------------------------
# Typer adapter
# -----------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def _list_command(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a JSON envelope instead of a table. Stable for scripts.",
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

    Thin adapter: pack the parsed options into a namespace, call
    ``run``, and translate the result into Typer's exit semantics.

    The translation is explicit rather than delegating to Typer's own
    exception classification, because Typer's usage-error handling
    only applies to exceptions raised *during argument parsing*. An
    exception raised from inside the callback body is not caught by
    that machinery; under ``standalone_mode=False`` (which
    ``forge.cli.main`` uses), it produces a silent exit 1. Rendering
    the message here and raising ``typer.Exit(code=2)`` gives the
    user the same observable behaviour Typer's own parser would have
    produced — usage-error message, exit code 2 — with no dependence
    on which of Typer's internal paths happens to catch the raise.
    """
    try:
        exit_code = run(SimpleNamespace(json=json_output, category=category, source=source))
    except ValueError as exc:
        # ``_validate_filters`` raises ``ValueError`` (a plain
        # exception usable outside the framework). Render it as a
        # usage error and exit with the POSIX-conventional code 2
        # (also the code Typer uses for its own parser errors).
        #
        # Why not ``typer.BadParameter``? See the adapter docstring:
        # ``BadParameter`` raised here is inside the callback body,
        # not during argument parsing, so Typer's parser-level error
        # handling does not catch it. The observable result was exit
        # code 1 with empty stderr — the worst possible UX for a
        # usage error, since the user sees no message and cannot
        # diagnose what went wrong.
        ui.error(str(exc))
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=exit_code)


# -----------------------------------------------------------------------------
# Filtering
# -----------------------------------------------------------------------------
def _validate_filters(*, category: str, source: str) -> None:
    """Reject unknown filter values before discovery runs.

    A typo like ``--category apis`` is a *usage* error, not a
    "nothing matched" outcome. Validating up front means the user sees
    an actionable message (``unknown category 'apis'``) rather than a
    silently-empty table that would send them looking for the wrong
    problem. It also means the validation cost is paid once, not per
    call site that filters.

    Raises
    ------
    ValueError
        If ``category`` or ``source`` is neither ``"all"`` nor a
        recognised value. ``ValueError`` (rather than
        ``typer.BadParameter``) is deliberate: this function is called
        from ``run``, which must be usable outside the Typer framework
        (tests, future HTTP endpoints, editor integrations). The Typer
        adapter renders the message and raises ``typer.Exit(code=2)``
        so the CLI's user-facing behaviour matches Typer's own parser
        errors, without depending on Typer's classification of
        exceptions raised from inside the callback body.
    """
    normalised_category = category.strip().lower()
    normalised_source = source.strip().lower()

    valid_categories = {c.value for c in Category}
    if normalised_category != _ALL and normalised_category not in valid_categories:
        raise ValueError(
            f"unknown category {category!r}; "
            f"expected one of {', '.join(sorted(valid_categories))} "
            f"or {_ALL!r}"
        )

    valid_sources = {s.value for s in BlueprintSource}
    if normalised_source != _ALL and normalised_source not in valid_sources:
        raise ValueError(
            f"unknown source {source!r}; "
            f"expected one of {', '.join(sorted(valid_sources))} "
            f"or {_ALL!r}"
        )


def _apply_filters(
    pairs: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
    *,
    category: str,
    source: str,
) -> list[tuple[DiscoveredBlueprint, BlueprintStatus]]:
    """Apply CLI filters to a discovered catalogue.

    Accepts the ``(DiscoveredBlueprint, BlueprintStatus)`` pairs that
    ``discover_with_status()`` returns, so callers do not have to
    re-correlate the two lists after filtering. ``_validate_filters``
    has already rejected unknown values, so this function's job is
    purely to narrow the list.

    Order is preserved from the input; the caller is responsible for
    sorting. This is deliberate: the function has no opinion about
    ordering, and a hidden sort here would silently double-sort when
    the caller sorts again (or silently disagree with the caller's
    sort if the two used different keys). Sorting belongs to exactly
    one place — ``run``.
    """
    normalised_category = category.strip().lower()
    normalised_source = source.strip().lower()

    result = pairs
    if normalised_category != _ALL:
        result = [(bp, status) for bp, status in result if bp.category.value == normalised_category]
    if normalised_source != _ALL:
        result = [(bp, status) for bp, status in result if bp.source.value == normalised_source]
    return result


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = ["app", "run"]
