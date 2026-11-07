"""Command discovery and registration for Forge CLI.

This module is the single place that knows *how* to find commands. It
scans the ``forge.cli.commands`` package for modules that follow the
command module contract, and registers each one with the root
``Typer`` application.

It is also the public seam the interactive ``create`` wizard uses to
enumerate and load blueprints. The wizard must not reach into the
blueprint loader's internals — it calls ``list_blueprints()`` for the
selection menu and ``get_blueprint()`` for per-variable prompts. Both
go through the same cache the rest of the CLI uses, so a blueprint
that appears in the menu is guaranteed to be loadable by the command
that consumes it.

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
of the underlying loader. The reasons:

- **Single source of truth.** The wizard, the non-interactive path,
  and any future web entry point all resolve blueprints through the
  same functions. A blueprint that appears in the menu is guaranteed
  to be loadable by the command that consumes it.
- **Testability.** A test can ``monkeypatch.setattr(registry,
  "_load_blueprint_index", ...)`` and exercise both the wizard and
  the non-interactive path against a fixture index, with no
  filesystem dependency.
- **Stability.** The wizard's menu is sorted (see
  ``list_blueprints``). Snapshot tests and human users both benefit
  from an order that does not reshuffle between runs.

Delegation, not duplication
---------------------------
The blueprint index is owned by the loader module, not by the
registry. ``_load_blueprint_index`` below is a thin, mockable seam
that delegates to the loader. If the loader's API changes, only this
seam changes — the wizard and the tests keep working.

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
- ``_load_blueprint_index`` references ``forge.blueprints.load_index``,
  which is a *forward declaration* — the loader is a separate task.
  Two scoped ``# type: ignore`` comments (one per error) mark the
  forward-declared reference; see the function's docstring for the
  removal condition.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

import typer

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

    Kept separate from the loader's internal blueprint type so the
    registry's public contract does not leak the loader's internals.
    The wizard only needs ``id`` and ``variables``; the command layer
    only needs ``id``. Everything else is an implementation detail of
    the loader that callers should not depend on.

    Attributes:
        id: The blueprint's canonical ID (e.g., ``"python.fastapi.api"``).
        display_name: Human-readable name for the selection menu.
            Falls back to ``id`` when the blueprint does not declare
            one, so the wizard never has to special-case.
        variables: The blueprint's declared variable descriptors. The
            wizard iterates this list to prompt for each variable;
            the command layer passes it to the validator. Kept as a
            tuple for immutability — a caught ``BlueprintEntry`` is
            safe to pass across threads and store in fixtures.
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
    # the other at dispatch time.
    #
    # The check covers both registration shapes: a single-command
    # app is registered as a *command* on ``root_app`` (visible via
    # ``root_app.registered_commands``), while a multi-command app is
    # registered as a *group* (visible via
    # ``root_app.registered_groups``). A name collision in either
    # shape is a bug, so the loop checks both.
    for registered in root_app.registered_groups:
        if registered.name == command_name:
            reason = f"command name {command_name!r} is already registered " f"by another module"
            if strict:
                raise RuntimeError(
                    f"Command module {full_name!r} would shadow an "
                    f"existing command {command_name!r}. Rename the "
                    f"command or remove the duplicate module."
                )
            logger.warning("Skipping command module %r: %s", full_name, reason)
            return SkippedCommand(module_name=short_name, reason=reason)
    for registered_cmd in root_app.registered_commands:
        if registered_cmd.name == command_name:
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
# layer to enumerate and load blueprints. They delegate to the
# blueprint loader via ``_load_blueprint_index``, a thin seam kept in
# this module so tests can substitute a fixture index without touching
# the loader (which is tested separately, in its own module).
#
# Design choice: no caching in this module.
# -----------------------------------------
# The loader already caches the index (parsing blueprint.yaml files is
# expensive; re-parsing them on every prompt would be wasteful). Adding
# a second cache here would create two sources of truth and a
# cache-invalidation bug waiting to happen. This module is a *facade*,
# not a cache — it calls through on every invocation, and the loader
# decides whether the call is cheap.
def _load_blueprint_index() -> Mapping[str, Any]:
    """Load the blueprint index (a mapping of ID → blueprint object).

    Kept as a separate function — not an inline import — so tests can
    substitute a fixture index with ``monkeypatch.setattr``. The
    loader's own tests exercise the real filesystem; the registry's
    tests exercise the seam. This is the difference between "the
    loader reads the filesystem" (true, tested elsewhere) and "the
    wizard can list blueprints" (the wizard's concern, tested here).

    Why the two ``# type: ignore`` comments
    ----------------------------------------
    ``forge.blueprints.load_index`` is a *separate task* in the WBS.
    The module ``forge.blueprints`` exists today (it holds the
    blueprint data directories), but does not yet expose
    ``load_index``. That is a forward declaration of the seam, not a
    typo: the registry is written against the *interface* the loader
    will provide, so the loader can land without changing this
    module.

    Two narrow ``type: ignore`` comments make the forward declaration
    explicit to ``mypy``, one per error:

    - ``attr-defined`` on the import — the symbol does not exist on
      the module *yet*.
    - ``no-any-return`` on the return — because ``load_index`` is
      unknown to ``mypy``, its return type is inferred as ``Any``,
      and the declared return type here is ``Mapping[str, Any]``.

    Both ignores are scoped to the two lines that reference the
    missing symbol. They are *not* module-wide, and they are *not*
    configured in ``pyproject.toml`` — a package-level
    ``ignore_missing_imports`` would hide future *typos* in the
    loader's own API once it lands, which is the opposite of what
    we want. A single-line ignore fails loudly when the code moves
    on; a package-level override fails silently forever.

    Both ignores are removed when the loader task lands. The
    docstring's mention of "forward declaration" is the removal
    note — grep for it to find every place that needs updating.

    Why not a ``Protocol``
    ----------------------
    A ``Protocol`` describing ``load_index``'s signature would also
    satisfy ``mypy``, but it requires *defining* the signature here
    (or in ``forge.blueprints``), which is more code than the loader
    itself will need. The two ignores are the minimum change; the
    ``Protocol`` becomes a natural cleanup once the loader's return
    type is concrete and worth formalizing.

    Why not a ``try/except ImportError`` fallback
    ---------------------------------------------
    Returning an empty mapping on ``ImportError`` would make the
    registry silently produce an empty blueprint menu when the
    installation is broken — a plausible-looking state that is
    actually a bug. The loud ``ImportError`` (exit 5) tells the user
    that something is wrong with the install, which is what
    ``--debug`` and the generic internal-error message are for. A
    dead end disguised as a valid state is the worst failure mode.

    Returns:
        A mapping of blueprint ID to the loader's blueprint object.

    Raises:
        ImportError: If the loader module is not importable. This is a
            packaging error, not a user error, so it propagates.
    """
    # Forward declaration of the loader's entry point; see the
    # docstring's "Why the two ``# type: ignore`` comments" section.
    # ``attr-defined``: the symbol is not on the module yet.
    from forge.blueprints import load_index  # type: ignore[attr-defined]

    # ``no-any-return``: ``load_index`` is unknown to ``mypy``, so its
    # return is inferred as ``Any``; the declared return type is
    # ``Mapping[str, Any]``.
    return load_index()  # type: ignore[no-any-return]


def list_blueprints() -> list[str]:
    """Return the sorted IDs of every available blueprint.

    Used by the interactive ``create`` wizard to build the selection
    menu. Sorted output is deliberate:

    - Users get a menu whose order does not shuffle between runs.
    - Snapshot tests can assert on exact output without flakiness.
    - The first item is stable, so the default selection is stable.

    An empty list is a valid return value — it means the installation
    has no blueprints. The wizard treats that as an ``UserInputError``
    with a clear hint, not a crash.

    Returns:
        A sorted list of blueprint IDs (e.g., ``["python.fastapi.api",
        "typescript.react.app"]``).
    """
    return sorted(_load_blueprint_index().keys())


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
            index. The error is domain-level (exit code 2), so the CLI
            handler renders it as "the blueprint is missing or
            malformed" without any special-casing here.
    """
    from forge.core.exceptions import BlueprintError

    index = _load_blueprint_index()
    blueprint = index.get(blueprint_id)
    if blueprint is None:
        available = ", ".join(sorted(index.keys())) or "(none installed)"
        raise BlueprintError(
            f"Blueprint {blueprint_id!r} is not installed.",
            hint=f"Available blueprints: {available}.",
        )

    # The loader's blueprint object exposes ``variables`` as an
    # iterable of descriptors, and optionally a ``display_name``.
    # ``getattr`` with a fallback keeps the registry forward-
    # compatible: a loader that adds ``display_name`` later will not
    # require a change here, and one that never adds it still works.
    variables = tuple(getattr(blueprint, "variables", ()))
    display_name = getattr(blueprint, "display_name", None) or blueprint_id

    return BlueprintEntry(
        id=blueprint_id,
        display_name=display_name,
        variables=variables,
    )


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
    "get_blueprint",
    "list_blueprints",
    "register_commands",
]
