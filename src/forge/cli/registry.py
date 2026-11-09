"""Command discovery and registration for Forge CLI.

This module is the single place that knows *how* to find commands. It
scans the ``forge.cli.commands`` package for modules that follow the
command module contract, and registers each one with the root ``Typer``
application.

It is also the public seam the interactive ``create`` wizard uses to
enumerate and load blueprints. The wizard must not reach into the
blueprint loader's internals — it calls ``list_blueprints()`` for the
selection menu and ``get_blueprint()`` for per-variable prompts. Both
delegate to ``forge.discovery.BlueprintDiscoveryService``, so the same
precedence rules (local overrides official, malformed skipped with
warnings) apply whether the caller is the wizard, the non-interactive
path, or ``forge list``.

The contract
------------
A *command module* is a Python module inside ``forge.cli.commands``
that exposes a module-level attribute named ``app`` whose value is a
``typer.Typer`` instance. Nothing else is required. The registry does
not care what commands the module defines, what arguments they take,
or how they are organised — it only cares that the module exposes a
Typer app.

The contract is documented in ``forge.cli.commands.__init__`` because
that is where new command authors will look. It is enforced here
because that is where the registry needs to know what to do with a
module that violates it.

Two shapes of command module
----------------------------
The registry supports both patterns Typer allows for a command module,
without requiring a module to declare which pattern it uses:

- **Single-command app.** The module exposes a ``Typer`` with a
  ``@app.callback()`` that carries the command's arguments and options,
  and no ``@app.command()`` subcommands. This is the shape of every
  module in this project. For this shape, the registry registers the
  *callback function* directly on the root app as a command — not the
  app as a sub-``Typer``. The distinction matters: ``add_typer`` on a
  callback-only app produces a *group* whose first positional is
  consumed by the callback and whose second token is treated as a
  subcommand, so ``forge create python.fastapi.api`` fails with
  ``Missing argument 'BLUEPRINT'`` even when the argument is present.
  Registering the callback directly produces a *leaf command*, which
  is what the docs promise and what the tests assert.

- **Multi-command app.** The module exposes a ``Typer`` with one or
  more ``@app.command()`` subcommands. For this shape, the registry
  calls ``add_typer``, which produces ``forge <name> <subcommand> …``
  — the correct shape for a module that genuinely exposes several
  commands.

The detection is done by ``_is_single_command_app`` using Typer's
public API (``registered_callback``, ``registered_commands``), so no
private attributes are touched.

Design principles
-----------------
1. **Open/Closed.** Adding a command means adding a file. The registry
   is never edited to add a command.
2. **Convention over configuration.** The rule is "expose ``app``"; no
   registration list, no decorator, no entry point.
3. **Graceful by default, strict when asked.** By default, a broken
   module produces a warning and is skipped, so ``forge --help`` never
   crashes because of one bad command. Tests pass ``strict=True`` to
   turn warnings into exceptions, so CI catches the broken module.
4. **Deterministic.** Modules are registered in sorted order, so the
   help output is stable across runs and platforms. Blueprint IDs are
   likewise returned sorted, for the same reason applied to the
   interactive wizard's selection menu.
5. **Testable in isolation.** The registry has no dependency on the
   root app, the CLI entry point, or Typer's runtime. It can be
   exercised with a mock package and a mock app.

Blueprint access (dual-mode input handling)
-------------------------------------------
The interactive ``create`` wizard needs two things from the registry
that are not related to *command* discovery:

- A list of blueprint IDs to display in a selection menu.
- A single blueprint object (with its declared variables) to drive
  per-variable prompts.

Both are exposed as public functions here — ``list_blueprints()`` and
``get_blueprint()`` — rather than as private helpers or direct imports
of the discovery service. The reasons:

- **Single source of truth.** The wizard, the non-interactive path,
  ``forge list``, and any future web entry point all resolve blueprints
  through the same service. A blueprint that appears in the menu is
  guaranteed to be loadable by the command that consumes it.
- **Testability.** A test can monkeypatch
  ``_make_discovery_service`` to return a fixture service, and exercise
  both the wizard and the non-interactive path with no filesystem
  dependency.
- **Stability.** ``list_blueprints`` returns IDs in sorted order (the
  service guarantees this for the full catalogue; the registry
  re-sorts defensively because it is a public contract). Snapshot
  tests and human users both benefit from an order that does not
  reshuffle between runs.

Delegation, not duplication
---------------------------
Blueprint discovery is owned by ``forge.discovery``, not by the
registry. ``_make_discovery_service`` below is a thin, mockable seam
that returns a fresh service per call. It exists so tests can
substitute a fixture service without touching the real filesystem, and
so a future config file (e.g. ``--blueprint-dir``) can override the
default ``.forge/blueprints`` location without changing any call site
outside this module.

**No caching in this module.** The service deliberately re-resolves on
every call so user edits are picked up immediately (see ADR-0009).
Adding a cache here would create two sources of truth and a
cache-invalidation bug waiting to happen. This module is a *facade*,
not a cache — it calls through on every invocation, and the service
decides whether the call is cheap.

Type-checking notes
-------------------
The file is checked with ``mypy --strict`` (see ``pyproject.toml``).
Three spots need explicit annotation to satisfy the checker without
weakening it:

- ``_register_single_command`` receives a ``Typer`` whose
  ``registered_callback.callback`` is typed
  ``Callable[..., Any] | None``. The runtime check inside that
  function narrows the type at the call site; see the function's
  docstring.
- ``get_blueprint`` constructs a ``BlueprintEntry`` whose
  ``variables`` field is ``tuple[Any, ...]``. The discovery service
  returns ``Blueprint`` objects with a typed ``variables`` list; the
  ``tuple(...)`` conversion keeps the entry immutable and the ``Any``
  in the field type keeps the registry decoupled from the model
  layer's concrete ``VariableDefinition`` class (which the loader may
  change without forcing a registry release).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

import typer

from forge.core.exceptions import BlueprintError
from forge.discovery import BlueprintDiscoveryService
from forge.models.blueprint import Blueprint

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
COMMANDS_PACKAGE: Final[str] = "forge.cli.commands"
"""Dotted path to the package that contains command modules.

Exposed as a module-level constant so tests can override it via
``patch`` without touching the registry's internals.
"""

APP_ATTRIBUTE: Final[str] = "app"
"""The attribute name a command module must expose.

This is the *entire* contract. Everything else is free-form.
"""


# -----------------------------------------------------------------------------
# Result model
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class RegisteredCommand:
    """A successfully registered command module.

    Attributes:
        module_name: The short module name (e.g., ``"list"``).
        full_name: The fully-qualified module name (e.g.,
            ``"forge.cli.commands.list"``).
        command_name: The name under which the command is mounted on
            the root app (e.g., ``"list"``). Derived from the module
            name by default, but a module can override it by setting
            ``app.info.name``.
    """

    module_name: str
    full_name: str
    command_name: str


@dataclass(frozen=True)
class SkippedCommand:
    """A module that was found but not registered.

    Attributes:
        module_name: The short module name.
        reason: A human-readable explanation of why it was skipped.
    """

    module_name: str
    reason: str


@dataclass(frozen=True)
class BlueprintEntry:
    """A loaded blueprint, as exposed to the command layer.

    Kept separate from the discovery service's internal ``Blueprint``
    model so the registry's public contract does not leak the model
    layer's internals. The wizard only needs ``id`` and ``variables``;
    the command layer only needs ``id``. Everything else is an
    implementation detail that callers should not depend on.

    Attributes:
        id: The blueprint's canonical ID (e.g.,
            ``"python.fastapi.api"``).
        display_name: Human-readable name for the selection menu.
            Falls back to ``id`` when the blueprint does not declare
            one, so the wizard never has to special-case.
        variables: The blueprint's declared variable descriptors. The
            wizard iterates this list to prompt for each variable; the
            command layer passes it to the validator. Kept as a tuple
            for immutability — a caught ``BlueprintEntry`` is safe to
            pass across threads and store in fixtures.
    """

    id: str
    display_name: str
    variables: tuple[Any, ...]


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------
def discover_command_modules(
    package: str = COMMANDS_PACKAGE,
) -> list[str]:
    """Return the names of every module in the commands package.

    The result is sorted for determinism. Private modules (names
    starting with ``_``) are excluded — they are implementation
    details of the package, not commands.

    Args:
        package: Dotted path to the package to scan. Defaults to
            ``forge.cli.commands``. Overridable for testing.

    Returns:
        A sorted list of short module names (e.g., ``["create",
        "doctor", "list", "ping", "validate", "verify"]``).
    """
    try:
        package_module = importlib.import_module(package)
    except ImportError as exc:
        # The package does not exist. This should never happen in a
        # well-formed install, but if it does, we want a clear error
        # rather than a silent empty registry.
        raise RuntimeError(
            f"Command package {package!r} could not be imported. "
            f"The package is required for command registration. "
            f"Original error: {exc}"
        ) from exc

    if not hasattr(package_module, "__path__"):
        raise RuntimeError(
            f"Command package {package!r} is not a package "
            f"(it has no __path__ attribute). Did you mean to point "
            f"at a module instead?"
        )

    names = [
        name
        for _, name, _ in pkgutil.iter_modules(
            package_module.__path__,
            prefix=f"{package}.",
        )
        # Skip private modules. A module named ``_helpers.py`` is not
        # a command; it is a helper that the command modules import.
        if not name.rsplit(".", 1)[-1].startswith("_")
    ]
    names.sort()
    return names


# -----------------------------------------------------------------------------
# Registration helpers
# -----------------------------------------------------------------------------
def _get_typer_app(module: ModuleType) -> typer.Typer | None:
    """Return the module's ``app`` attribute if it is a Typer app.

    Returns ``None`` if the module does not expose ``app`` or if the
    attribute is not a ``typer.Typer`` instance. The caller decides
    whether that is a skip or an error.
    """
    candidate = getattr(module, APP_ATTRIBUTE, None)
    if candidate is None:
        return None
    if not isinstance(candidate, typer.Typer):
        return None
    return candidate


def _is_single_command_app(app: typer.Typer) -> bool:
    """Return ``True`` iff the app is a leaf command in disguise.

    A *single-command app* is a ``Typer`` whose behavior is entirely a
    callback and which has no registered subcommands. That is the
    shape of every module in this project — each command module
    exposes exactly one command — and it is the shape the registry
    must *not* mount with ``add_typer``, because ``add_typer`` would
    nest it (producing a group whose first positional is consumed by
    the callback and whose second token is treated as a subcommand).

    The check uses only Typer's public API:

    - ``registered_callback`` is the ``TyperCallback`` object created
      by ``@app.callback()`` (or ``None`` if no callback was
      registered). Its presence distinguishes "this app has a
      function that runs when the app is invoked" from "this app is
      just a container for subcommands."
    - ``registered_commands`` is the list of ``TyperCommand`` objects
      created by ``@app.command()``. Its length distinguishes "this
      app is a leaf" from "this app is a group."

    The moment a module registers even one subcommand, the app is
    genuinely a group, and ``add_typer`` is the correct path — even if
    the app also has a callback. That is why the check is ``has
    callback AND no commands``, not just ``has callback``: an app
    with both is a group with an entry point, and the group-shaped
    registration is correct for it.
    """
    has_callback = app.registered_callback is not None
    has_no_commands = len(app.registered_commands) == 0
    return has_callback and has_no_commands


def _register_single_command(
    root_app: typer.Typer,
    app: typer.Typer,
    command_name: str,
) -> None:
    """Register a single-command app's callback as a leaf command.

    ``root_app.command(name)(callback_fn)`` is the same call that
    ``@root_app.command(name=...)`` would make — it registers the
    callback's *function* as a command whose signature is the
    function's signature. That is exactly what we want: the callback
    already carries the arguments and options; registering it as a
    command (rather than as a sub-``Typer``) produces ``forge
    <command_name> <args>`` directly, with no nesting.

    The alternative — ``root_app.add_typer(app, name=command_name)``
    — is only correct for a multi-command app. See
    ``_is_single_command_app`` for the distinction.

    Why the runtime check on ``callback.callback``
    ----------------------------------------------
    Typer types ``app.registered_callback`` as ``TyperCallback | None``,
    and ``TyperCallback.callback`` as ``Callable[..., Any] | None``.
    Two levels of ``Optional`` exist because ``TyperCallback`` is a
    public class that can be constructed without a function in some
    Typer code paths.

    ``_is_single_command_app`` guarantees *both* levels are non-``None``
    before this function is called, but ``mypy`` cannot see across
    function boundaries. Without a runtime check, ``mypy --strict``
    rejects ``root_app.command(name)(callback.callback)`` with:

        Value of type variable "CommandFunctionType" of function
        cannot be "Callable[..., Any] | None"

    The check below narrows the type at the exact call site. It
    combines the two ``None`` checks into a single ``or`` expression
    so that removing one check does not leave the other — the two
    levels of ``Optional`` are guarded together, because they matter
    together.

    The branch is also a defensive assertion against a future
    refactor that weakens ``_is_single_command_app`` without
    updating this function: the failure is caught here, at the
    point where the missing callback first matters, with a message
    that names the invariant. Unlike an ``assert``, it survives
    ``python -O`` and does not violate ``S101`` (flake8-bandit's
    no-assert rule).

    Args:
        root_app: The root ``typer.Typer`` to register the command on.
        app: The single-command ``Typer`` whose callback is the
            command.
        command_name: The name to mount the command under. Derived
            from ``app.info.name`` by the caller.

    Raises:
        RuntimeError: If ``app.registered_callback`` or its
            ``.callback`` field is ``None``. The caller guarantees
            both are set by calling this function only when
            ``_is_single_command_app(app)`` is ``True``; the raise is
            a defensive check against a future refactor that weakens
            the detector without updating this function.
    """
    registered_callback = app.registered_callback
    if registered_callback is None or registered_callback.callback is None:
        # ``pragma: no cover`` — unreachable by construction. See
        # the docstring's "Why the runtime check on
        # ``callback.callback``" section for why this branch exists
        # despite being unreachable.
        raise RuntimeError(  # pragma: no cover
            "_register_single_command called for a Typer without a "
            "registered callback function; _is_single_command_app "
            "must have returned True incorrectly."
        )
    root_app.command(command_name)(registered_callback.callback)


def _command_name_collides(
    root_app: typer.Typer,
    command_name: str,
) -> bool:
    """Return ``True`` iff ``command_name`` is already mounted on ``root_app``.

    Checks both registration shapes:

    - ``registered_commands`` covers single-command apps mounted as
      *leaf commands* (see ``_register_single_command``).
    - ``registered_groups`` covers multi-command apps mounted as
      *sub-Typers* (see ``add_typer``).

    A name collision in either shape would cause one module to shadow
    the other at dispatch time, so both are checked. Extracted into a
    helper so the strict and non-strict branches in ``_register_one``
    cannot drift apart — the collision rule lives in exactly one place.

    Implemented with two ``any(...)`` expressions, not a loop.
    ``any`` short-circuits on the first match, so the runtime cost is
    identical to the loop it replaces. The expression form also
    removes the `return True`/`return False` pair, which is where
    accidental `return None` bugs in collision checks typically hide.
    """
    return any(cmd.name == command_name for cmd in root_app.registered_commands) or any(
        group.name == command_name for group in root_app.registered_groups
    )


def _register_one(
    root_app: typer.Typer,
    full_name: str,
    strict: bool,
) -> RegisteredCommand | SkippedCommand:
    """Register a single command module with the root app.

    Args:
        root_app: The root ``typer.Typer`` to register the command on.
        full_name: Fully-qualified module name (e.g.,
            ``"forge.cli.commands.list"``).
        strict: If ``True``, any skip becomes a ``RuntimeError``. If
            ``False``, the skip is logged and returned.

    Returns:
        ``RegisteredCommand`` on success, ``SkippedCommand`` on skip.

    Raises:
        RuntimeError: If ``strict=True`` and the module is skipped.
    """
    short_name = full_name.rsplit(".", 1)[-1]

    try:
        module = importlib.import_module(full_name)
    except Exception as exc:  # — we re-raise under strict
        reason = f"module failed to import: {exc}"
        if strict:
            raise RuntimeError(
                f"Command module {full_name!r} failed to import. "
                f"Fix the import error, or remove the module. "
                f"Underlying error: {exc}"
            ) from exc
        logger.warning("Skipping command module %r: %s", full_name, reason)
        return SkippedCommand(module_name=short_name, reason=reason)

    app = _get_typer_app(module)
    if app is None:
        reason = (
            f"module does not expose a module-level "
            f"{APP_ATTRIBUTE!r} attribute of type typer.Typer"
        )
        if strict:
            raise RuntimeError(
                f"Command module {full_name!r} violates the command "
                f"module contract: {reason}. See "
                f"forge.cli.commands.__init__ for the contract."
            )
        logger.warning("Skipping command module %r: %s", full_name, reason)
        return SkippedCommand(module_name=short_name, reason=reason)

    # Derive the command name. Typer stores the name on the app; if
    # the app was constructed without an explicit name, Typer uses
    # the module name. Either way, ``app.info.name`` is the source of
    # truth after construction.
    command_name = app.info.name or short_name

    # Detect duplicate registrations. Two modules mounting the same
    # command name is a bug that would cause one to silently shadow
    # the other at dispatch time. See ``_command_name_collides`` for
    # which shapes are covered and why.
    if _command_name_collides(root_app, command_name):
        reason = f"command name {command_name!r} is already registered " f"by another module"
        if strict:
            raise RuntimeError(
                f"Command module {full_name!r} would shadow an "
                f"existing command {command_name!r}. Rename the "
                f"command or remove the duplicate module."
            )
        logger.warning("Skipping command module %r: %s", full_name, reason)
        return SkippedCommand(module_name=short_name, reason=reason)

    # -------------------------------------------------------------------------
    # Registration path — single-command vs. multi-command
    # -------------------------------------------------------------------------
    # See the module docstring's "Two shapes of command module" and
    # ``_is_single_command_app`` for why this branch exists. The short
    # version: a single-command app is a leaf command in disguise, and
    # ``add_typer`` would nest it. Registering the callback directly
    # produces the leaf command the user expects.
    if _is_single_command_app(app):
        _register_single_command(root_app, app, command_name)
    else:
        root_app.add_typer(app, name=command_name)

    return RegisteredCommand(
        module_name=short_name,
        full_name=full_name,
        command_name=command_name,
    )


def register_commands(
    root_app: typer.Typer,
    *,
    package: str = COMMANDS_PACKAGE,
    strict: bool = False,
    on_skip: Callable[[SkippedCommand], None] | None = None,
) -> list[RegisteredCommand | SkippedCommand]:
    """Discover and register every command module on the root app.

    This is the public entry point of the registry. It is called once
    from ``forge.cli.main.create_app``. It is also called from tests,
    which pass ``strict=True`` to catch contract violations early.

    Args:
        root_app: The root ``typer.Typer`` to register commands on.
        package: Dotted path to the package to scan. Defaults to
            ``forge.cli.commands``. Overridable for testing.
        strict: If ``True``, any skip becomes a ``RuntimeError``. If
            ``False`` (the default), skips are logged and returned.
        on_skip: Optional callback invoked for each skipped module.
            Useful for tests that want to assert "these modules were
            skipped, for these reasons" without parsing log output.

    Returns:
        A list of ``RegisteredCommand`` and ``SkippedCommand`` objects,
        in discovery order (sorted by module name). The caller can
        inspect the list to see what was registered and what was not.

    Raises:
        RuntimeError: If ``strict=True`` and any module is skipped.
    """
    full_names = discover_command_modules(package)
    results: list[RegisteredCommand | SkippedCommand] = []

    for full_name in full_names:
        result = _register_one(root_app, full_name, strict=strict)
        results.append(result)
        if isinstance(result, SkippedCommand) and on_skip is not None:
            on_skip(result)

    return results


# -----------------------------------------------------------------------------
# Blueprint access — public seam for the interactive create wizard
# -----------------------------------------------------------------------------
# The functions below are the *only* supported way for the command
# layer to enumerate and load blueprints. They delegate to
# ``forge.discovery.BlueprintDiscoveryService`` via
# ``_make_discovery_service``, a thin seam kept in this module so
# tests can substitute a fixture service without touching the real
# filesystem.
#
# Design choice: no caching in this module.
# -----------------------------------------
# The service deliberately re-resolves on every call so user edits are
# picked up immediately (see ADR-0009's "cache policy" section). Adding
# a second cache here would create two sources of truth and a
# cache-invalidation bug waiting to happen. This module is a *facade*,
# not a cache — it calls through on every invocation, and the service
# decides whether the call is cheap.
#
# Design choice: seam, not direct import.
# ---------------------------------------
# Tests substitute ``_make_discovery_service`` to exercise the wizard
# and non-interactive paths against a fixture catalogue. The real
# service is tested separately in ``forge.discovery``'s own test
# suite. Keeping the seam here means a future config file (e.g.
# ``--blueprint-dir``) can override the default ``.forge/blueprints``
# location without changing any call site outside this module.
def _make_discovery_service() -> BlueprintDiscoveryService:
    """Construct a fresh discovery service.

    Kept as a function — not an inline call — so tests can substitute
    a fixture service with ``monkeypatch.setattr``. The service's own
    tests exercise the real filesystem; the registry's tests exercise
    the seam. This is the difference between "discovery reads the
    filesystem" (true, tested elsewhere) and "the wizard can list
    blueprints" (the wizard's concern, tested here).

    Returning a *fresh* service per call (rather than a module-level
    singleton) is deliberate: it means a test that patches
    ``_make_discovery_service`` cannot accidentally leak state into a
    test that does not patch it, and it matches the service's
    documented no-caching policy.

    When ``--blueprint-dir`` lands as a CLI flag, this is the single
    function that grows a parameter — every caller downstream of the
    registry stays unchanged.

    Returns:
        A ``BlueprintDiscoveryService`` configured with the default
        local root.
    """
    return BlueprintDiscoveryService()


def list_blueprints() -> list[str]:
    """Return the sorted IDs of every available blueprint.

    Used by the interactive ``create`` wizard to build the selection
    menu. Sorted output is deliberate:

    - Users get a menu whose order does not shuffle between runs.
    - Snapshot tests can assert on exact output without flakiness.
    - The first item is stable, so the default selection is stable.

    The service already returns a catalogue sorted by id (see
    ADR-0009); the registry re-sorts defensively because it is a
    public contract, and a future service change that drops the sort
    should not silently reshuffle the wizard.

    An empty list is a valid return value — it means the installation
    has no blueprints. The wizard treats that as a ``UserInputError``
    with a clear hint, not a crash.

    Returns:
        A sorted list of blueprint IDs (e.g., ``["python.fastapi.api",
        "typescript.frontend.react"]``).
    """
    catalogue = _make_discovery_service().discover()
    return sorted(bp.id for bp in catalogue)


def get_blueprint(blueprint_id: str) -> BlueprintEntry:
    """Load a single blueprint by ID, as a command-layer view.

    Used by the interactive ``create`` wizard to obtain the variable
    descriptors it will prompt for, and by the non-interactive path to
    validate flag values against the same descriptors. The shared
    accessor is deliberate: the wizard and the flag path must see
    *identical* rules, or the two modes drift — the exact technical
    debt the dual-mode design exists to prevent.

    Args:
        blueprint_id: The blueprint's canonical ID (e.g.,
            ``"python.fastapi.api"``).

    Returns:
        A ``BlueprintEntry`` with the ID, display name, and declared
        variables.

    Raises:
        BlueprintError: If the blueprint ID is not present in the
            catalogue. The error is domain-level (exit code 2), so the
            CLI handler renders it as "the blueprint is missing or
            malformed" without any special-casing here.
    """
    service = _make_discovery_service()
    try:
        blueprint = service.load(blueprint_id)
    except Exception as exc:
        # The service raises ``DiscoveryError`` for a missing id; we
        # translate it to a ``BlueprintError`` with a richer message
        # (the list of available ids) so the CLI handler's output is
        # the same whether the user mistyped the id in a flag or
        # selected a stale entry from the wizard menu. Translating
        # here, rather than in the handler, keeps the "what error
        # shape does the CLI see?" question answerable in one place.
        available = ", ".join(list_blueprints()) or "(none installed)"
        raise BlueprintError(
            f"Blueprint {blueprint_id!r} is not installed.",
            hint=f"Available blueprints: {available}.",
        ) from exc

    # The model exposes ``variables`` as a list of typed
    # ``VariableDefinition`` objects. The registry's ``BlueprintEntry``
    # keeps them as ``tuple[Any, ...]`` so the registry is not coupled
    # to the model layer's concrete variable class — a future schema
    # change to ``VariableDefinition`` should not force a registry
    # release. Callers (the wizard, the validator) already treat the
    # descriptors structurally; typing them precisely would add a
    # dependency without adding safety.
    variables: tuple[Any, ...] = tuple(blueprint.variables)
    display_name = blueprint.name or blueprint.id

    return BlueprintEntry(
        id=blueprint.id,
        display_name=display_name,
        variables=variables,
    )


def get_blueprint_document(blueprint_id: str) -> Blueprint:
    """Load the full ``Blueprint`` document for ``blueprint_id``.

    Companion to ``get_blueprint``. The two differ in *what they return*
    because they serve different callers:

    * ``get_blueprint`` returns a ``BlueprintEntry`` — a catalogue view
      with ``id``, ``display_name``, and the raw variables tuple. That
      is what the interactive wizard needs to build a menu and
      per-variable prompts; it is *not* enough to resolve values
      against (the resolver needs the full document type, and reaching
      into the entry's variables tuple would duplicate the loader's
      schema validation at the call site).

    * ``get_blueprint_document`` returns the loaded ``Blueprint`` — the
      schema-validated, model-validated document. That is what any
      caller that *does something with* the blueprint needs:
      resolving variables (``forge create``), rendering templates
      (future), verifying checks (future).

    Both go through the same ``_make_discovery_service()`` seam, so a
    test that patches the seam substitutes both. The distinction is
    about *shape*, not about *source* — the source of truth for what
    a blueprint is remains the loader, and neither function bypasses
    it.

    Returns
    -------
    Blueprint
        The loaded, validated document.

    Raises
    ------
    BlueprintError
        If ``blueprint_id`` is not present in the catalogue, or if the
        blueprint's document fails to load. Same domain-level contract
        as ``get_blueprint`` — the CLI handler routes both to exit
        code 2 without special-casing either.
    """
    service = _make_discovery_service()
    try:
        return service.load(blueprint_id)
    except Exception as exc:
        available = ", ".join(list_blueprints()) or "(none installed)"
        raise BlueprintError(
            f"Blueprint {blueprint_id!r} is not installed.",
            hint=f"Available blueprints: {available}.",
        ) from exc


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = [
    "APP_ATTRIBUTE",
    "COMMANDS_PACKAGE",
    "BlueprintEntry",
    "RegisteredCommand",
    "SkippedCommand",
    "discover_command_modules",
    "get_blueprint_document",
    "get_blueprint",
    "list_blueprints",
    "register_commands",
]
