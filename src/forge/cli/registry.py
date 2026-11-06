"""Command discovery and registration for Forge CLI.

This module is the single place that knows *how* to find commands. It
scans the ``forge.cli.commands`` package for modules that follow the
command module contract, and registers each one with the root
``Typer`` application.

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
   help output is stable across runs and platforms.
5. **Testable in isolation.** The registry has no dependency on the
   root app, the CLI entry point, or Typer's runtime. It can be
   exercised with a mock package and a mock app.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Final

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
# Registration
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
# Public re-exports
# -----------------------------------------------------------------------------
__all__ = [
    "APP_ATTRIBUTE",
    "COMMANDS_PACKAGE",
    "RegisteredCommand",
    "SkippedCommand",
    "discover_command_modules",
    "register_commands",
]
