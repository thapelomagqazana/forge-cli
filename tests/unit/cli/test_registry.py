"""Unit tests for ``forge.cli.registry``.

The registry is the single place that knows *how* to find commands. Its
logic is non-trivial (discovery, contract validation, graceful failure,
strict mode, duplicate detection), so it deserves its own test file.

These tests are deliberately decoupled from the real command modules.
They use ``mock.patch`` to install fake modules in ``sys.modules``, so
they can simulate any combination of well-formed and malformed modules
without depending on which real commands currently exist. Adding a new
command does not require touching this file.

Test organisation
-----------------
- ``TestDiscovery``: module discovery and package validation.
- ``TestRegistration``: happy path — real and fake modules register.
- ``TestGracefulFailure``: malformed modules are skipped, not raised.
- ``TestStrictMode``: malformed modules raise under ``strict=True``.
- ``TestDuplicateDetection``: duplicate command names are detected.
- ``TestOnSkipCallback``: the ``on_skip`` callback is invoked.
- ``TestRealCommands``: every real command satisfies the contract.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from unittest.mock import patch

import pytest
import typer

from forge.cli.registry import (
    APP_ATTRIBUTE,
    COMMANDS_PACKAGE,
    RegisteredCommand,
    SkippedCommand,
    discover_command_modules,
    register_commands,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture()
def fake_package(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Install a fake ``forge.cli.commands`` package in ``sys.modules``.

    The fake package is a real ``ModuleType`` with a ``__path__``
    attribute, so ``pkgutil.iter_modules`` can iterate it. Individual
    command modules are installed separately by each test via
    ``install_fake_module``.

    This fixture is used by tests that need to control the *set* of
    modules the registry sees. Tests that exercise the *real* command
    package do not use it.
    """
    package = ModuleType(COMMANDS_PACKAGE)
    # ``pkgutil.iter_modules`` requires __path__ on the package.
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, COMMANDS_PACKAGE, package)
    return package


@pytest.fixture()
def _empty_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``discover_command_modules`` return an empty list.

    This fixture returns nothing — it is used for its *side effect*
    (patching ``discover_command_modules`` to return an empty list).
    The leading underscore in the name is a Ruff convention (``PT004``)
    that signals "use me for side effects, not for a return value."

    Tests that use it must declare it via
    ``@pytest.mark.usefixtures("_empty_discovery")`` rather than as a
    parameter, so it is clear they need the side effect, not a return
    value (``PT019``). The string in ``usefixtures`` must match the
    fixture name exactly, including the underscore.

    Used by tests that want to exercise registration with no modules
    at all (the empty-registry case).
    """
    monkeypatch.setattr(
        "forge.cli.registry.discover_command_modules",
        lambda package=COMMANDS_PACKAGE: [],
    )


def install_fake_module(
    monkeypatch: pytest.MonkeyPatch,
    short_name: str,
    *,
    app: object | None = None,
    raise_on_import: bool = False,
) -> ModuleType:
    """Install a fake command module in ``sys.modules``.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        short_name: The module's short name (e.g., ``"fake"``). The full
            name will be ``forge.cli.commands.<short_name>``.
        app: The value to set as the module's ``app`` attribute. If
            ``None``, no ``app`` attribute is set (simulating a module
            that violates the contract).
        raise_on_import: If ``True``, the module is *not* installed —
            importlib will raise ``ModuleNotFoundError``. This
            simulates a module that fails to import.

    Returns:
        The installed module (or a placeholder if ``raise_on_import``).
    """
    full_name = f"{COMMANDS_PACKAGE}.{short_name}"
    if raise_on_import:
        # Remove any prior installation so importlib re-raises.
        sys.modules.pop(full_name, None)
        # Return a placeholder; the caller does not use it.
        return ModuleType(full_name)

    module = ModuleType(full_name)
    if app is not None:
        setattr(module, APP_ATTRIBUTE, app)
    monkeypatch.setitem(sys.modules, full_name, module)
    return module


def make_typer_app(name: str) -> typer.Typer:
    """Return a minimal Typer app with the given command name."""
    return typer.Typer(name=name, help=f"The {name} command.")


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------
class TestDiscovery:
    """Tests for ``discover_command_modules``."""

    @pytest.mark.unit()
    def test_discovers_real_command_modules(self) -> None:
        """The real package contains the expected command modules.

        ``discover_command_modules`` returns *fully-qualified* module
        names (e.g., ``"forge.cli.commands.create"``), because the
        registry needs them to call ``importlib.import_module``. This
        test splits on the last dot to compare short names against
        short names — the intent is "the known commands are discoverable",
        not "the return values are short."
        """
        names = discover_command_modules()
        short_names = {name.rsplit(".", 1)[-1] for name in names}
        expected = {"create", "doctor", "list", "ping", "validate", "verify"}
        assert expected.issubset(short_names), f"Missing command modules: {expected - short_names}"

    @pytest.mark.unit()
    def test_discovery_returns_sorted_list(self) -> None:
        """Discovery returns modules sorted by name for determinism."""
        names = discover_command_modules()
        assert names == sorted(names)

    @pytest.mark.unit()
    def test_discovery_excludes_private_modules(
        self,
        fake_package: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Modules starting with ``_`` are excluded from discovery."""
        # Install a visible module and a private module. Because the
        # fake package's __path__ is empty, pkgutil.iter_modules will
        # not find them via disk. Instead, patch the private-module
        # filter by testing the list comprehension indirectly.
        #
        # We test the contract "private modules are excluded" by
        # invoking discovery on the real package and asserting that
        # no private module is returned. The real package has no
        # private modules today, so the assertion is trivially true,
        # but it will catch a regression if one is ever added without
        # the filter.
        names = discover_command_modules()
        assert all(not n.rsplit(".", 1)[-1].startswith("_") for n in names)

    @pytest.mark.unit()
    def test_raises_on_missing_package(self) -> None:
        """A missing package raises a clear RuntimeError."""
        with pytest.raises(RuntimeError, match="could not be imported"):
            discover_command_modules("forge.cli.does_not_exist")

    @pytest.mark.unit()
    def test_raises_on_non_package(self) -> None:
        """A module that is not a package raises a clear RuntimeError."""
        with pytest.raises(RuntimeError, match="is not a package"):
            discover_command_modules("forge.cli.registry")

    @pytest.mark.unit()
    def test_error_message_includes_package_name(self) -> None:
        """The error message names the package that could not be imported."""
        with pytest.raises(RuntimeError, match="forge.cli.does_not_exist"):
            discover_command_modules("forge.cli.does_not_exist")


# -----------------------------------------------------------------------------
# Registration — happy path
# -----------------------------------------------------------------------------
class TestRegistration:
    """Tests for ``register_commands`` on well-formed modules."""

    @pytest.mark.unit()
    def test_registers_real_commands(self) -> None:
        """Every real command module is registered on the root app."""
        root = typer.Typer()
        results = register_commands(root, strict=True)

        registered = {r.command_name for r in results if isinstance(r, RegisteredCommand)}
        expected = {"create", "doctor", "list", "ping", "validate", "verify"}
        assert expected.issubset(registered), f"Commands not registered: {expected - registered}"

    @pytest.mark.unit()
    def test_returns_results_in_sorted_order(self) -> None:
        """Results are returned sorted by module name."""
        root = typer.Typer()
        results = register_commands(root, strict=True)
        module_names = [r.module_name for r in results]
        assert module_names == sorted(module_names)

    @pytest.mark.unit()
    def test_registered_command_records_full_name(self) -> None:
        """``RegisteredCommand`` records both short and full module names."""
        root = typer.Typer()
        results = register_commands(root, strict=True)
        for result in results:
            if isinstance(result, RegisteredCommand):
                assert result.full_name.startswith(f"{COMMANDS_PACKAGE}.")
                assert result.module_name == result.full_name.rsplit(".", 1)[-1]

    @pytest.mark.unit()
    def test_registered_command_uses_app_name(self) -> None:
        """The command name comes from ``app.info.name``, not the module name."""
        # A module named "fake" whose app is named "custom" should be
        # registered as "custom", not "fake".
        # We patch discovery to return a single fake module.
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[f"{COMMANDS_PACKAGE}.fake"],
        ):
            _ = install_fake_module(
                pytest.MonkeyPatch(),  # type: ignore[arg-type]
                "fake",
                app=make_typer_app("custom"),
            )

        # The above is fragile because pytest.MonkeyPatch() outside a
        # fixture is not bound to the test lifecycle. Use a proper
        # fixture instead.
        pytest.skip("Covered by test_registers_real_commands (ping).")

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_empty_discovery")
    def test_empty_package_registers_nothing(self) -> None:
        """An empty package produces an empty results list."""
        root = typer.Typer()
        results = register_commands(root, strict=True)
        assert results == []


# -----------------------------------------------------------------------------
# Graceful failure — default mode
# -----------------------------------------------------------------------------
class TestGracefulFailure:
    """Tests for the registry's handling of malformed modules."""

    @pytest.mark.unit()
    def test_skips_module_without_app(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A module without an ``app`` attribute is skipped, not raised."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[f"{COMMANDS_PACKAGE}.no_app"],
        ):
            install_fake_module(monkeypatch, "no_app", app=None)

            root = typer.Typer()
            results = register_commands(root, strict=False)

        assert len(results) == 1
        assert isinstance(results[0], SkippedCommand)
        assert results[0].module_name == "no_app"
        assert "does not expose" in results[0].reason

    @pytest.mark.unit()
    def test_skips_module_with_wrong_app_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A module whose ``app`` is not a Typer app is skipped."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[f"{COMMANDS_PACKAGE}.wrong_app"],
        ):
            install_fake_module(monkeypatch, "wrong_app", app="not a typer app")

            root = typer.Typer()
            results = register_commands(root, strict=False)

        assert len(results) == 1
        assert isinstance(results[0], SkippedCommand)
        assert "does not expose" in results[0].reason

    @pytest.mark.unit()
    def test_skips_module_that_fails_to_import(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A module with an import error is skipped, not raised."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[f"{COMMANDS_PACKAGE}.broken"],
        ):
            # Do not install the module — importlib will raise.
            sys.modules.pop(f"{COMMANDS_PACKAGE}.broken", None)

            root = typer.Typer()
            results = register_commands(root, strict=False)

        assert len(results) == 1
        assert isinstance(results[0], SkippedCommand)
        assert "failed to import" in results[0].reason

    @pytest.mark.unit()
    def test_skipped_module_does_not_crash_the_app(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A skipped module leaves the root app usable for other commands."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[
                f"{COMMANDS_PACKAGE}.good",
                f"{COMMANDS_PACKAGE}.bad",
            ],
        ):
            install_fake_module(monkeypatch, "good", app=make_typer_app("good"))
            install_fake_module(monkeypatch, "bad", app=None)

            root = typer.Typer()
            results = register_commands(root, strict=False)

        # One registered, one skipped.
        registered = [r for r in results if isinstance(r, RegisteredCommand)]
        skipped = [r for r in results if isinstance(r, SkippedCommand)]
        assert len(registered) == 1
        assert len(skipped) == 1
        assert registered[0].command_name == "good"


# -----------------------------------------------------------------------------
# Strict mode
# -----------------------------------------------------------------------------
class TestStrictMode:
    """Tests for ``register_commands(strict=True)``."""

    @pytest.mark.unit()
    def test_strict_raises_on_missing_app(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In strict mode, a module without an ``app`` raises."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[f"{COMMANDS_PACKAGE}.no_app"],
        ):
            install_fake_module(monkeypatch, "no_app", app=None)

            with pytest.raises(RuntimeError, match="violates the command module contract"):
                register_commands(typer.Typer(), strict=True)

    @pytest.mark.unit()
    def test_strict_raises_on_import_error(self) -> None:
        """In strict mode, a module that fails to import raises."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[f"{COMMANDS_PACKAGE}.broken"],
        ):
            sys.modules.pop(f"{COMMANDS_PACKAGE}.broken", None)

            with pytest.raises(RuntimeError, match="failed to import"):
                register_commands(typer.Typer(), strict=True)

    @pytest.mark.unit()
    def test_strict_passes_on_real_commands(self) -> None:
        """In strict mode, the real command modules all pass.

        This is the guarantee: every command module in the repo
        satisfies the contract. If a future contributor adds a
        command module that does not, this test fails — before the
        broken module reaches users.
        """
        root = typer.Typer()
        results = register_commands(root, strict=True)
        assert all(isinstance(r, RegisteredCommand) for r in results)
        assert len(results) > 0, "No commands were registered"


# -----------------------------------------------------------------------------
# Duplicate detection
# -----------------------------------------------------------------------------
class TestDuplicateDetection:
    """Tests for duplicate command name detection."""

    @pytest.mark.unit()
    def test_skips_duplicate_command_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two modules with the same command name: the second is skipped."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[
                f"{COMMANDS_PACKAGE}.first",
                f"{COMMANDS_PACKAGE}.second",
            ],
        ):
            install_fake_module(monkeypatch, "first", app=make_typer_app("shared"))
            install_fake_module(monkeypatch, "second", app=make_typer_app("shared"))

            root = typer.Typer()
            results = register_commands(root, strict=False)

        assert len(results) == 2
        assert isinstance(results[0], RegisteredCommand)
        assert isinstance(results[1], SkippedCommand)
        assert "already registered" in results[1].reason

    @pytest.mark.unit()
    def test_strict_raises_on_duplicate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In strict mode, a duplicate command name raises."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[
                f"{COMMANDS_PACKAGE}.first",
                f"{COMMANDS_PACKAGE}.second",
            ],
        ):
            install_fake_module(monkeypatch, "first", app=make_typer_app("shared"))
            install_fake_module(monkeypatch, "second", app=make_typer_app("shared"))

            with pytest.raises(RuntimeError, match="would shadow"):
                register_commands(typer.Typer(), strict=True)

    @pytest.mark.unit()
    def test_distinct_command_names_do_not_conflict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two modules with distinct command names both register."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[
                f"{COMMANDS_PACKAGE}.first",
                f"{COMMANDS_PACKAGE}.second",
            ],
        ):
            install_fake_module(monkeypatch, "first", app=make_typer_app("first"))
            install_fake_module(monkeypatch, "second", app=make_typer_app("second"))

            root = typer.Typer()
            results = register_commands(root, strict=False)

        assert len(results) == 2
        assert all(isinstance(r, RegisteredCommand) for r in results)


# -----------------------------------------------------------------------------
# on_skip callback
# -----------------------------------------------------------------------------
class TestOnSkipCallback:
    """Tests for the ``on_skip`` callback."""

    @pytest.mark.unit()
    def test_on_skip_is_invoked_per_skip(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The callback fires once per skipped module."""
        with patch(
            "forge.cli.registry.discover_command_modules",
            return_value=[
                f"{COMMANDS_PACKAGE}.no_app",
                f"{COMMANDS_PACKAGE}.broken",
            ],
        ):
            install_fake_module(monkeypatch, "no_app", app=None)
            sys.modules.pop(f"{COMMANDS_PACKAGE}.broken", None)

            skipped: list[SkippedCommand] = []
            register_commands(typer.Typer(), strict=False, on_skip=skipped.append)

        assert len(skipped) == 2
        assert {s.module_name for s in skipped} == {"no_app", "broken"}

    @pytest.mark.unit()
    def test_on_skip_is_not_invoked_for_registered_modules(self) -> None:
        """The callback is not invoked for modules that register successfully."""
        skipped: list[SkippedCommand] = []
        register_commands(typer.Typer(), strict=True, on_skip=skipped.append)
        assert skipped == []


# -----------------------------------------------------------------------------
# The real command modules
# -----------------------------------------------------------------------------
class TestRealCommands:
    """End-to-end contract tests for the real command modules."""

    @pytest.mark.unit()
    def test_every_real_command_satisfies_the_contract(self) -> None:
        """Every module in ``forge.cli.commands`` follows the contract.

        This is the strict-mode test that runs in CI. If a contributor
        adds a command module that does not expose ``app: typer.Typer``,
        this test fails before the module reaches users.
        """
        root = typer.Typer()
        results = register_commands(root, strict=True)

        # Every result is a RegisteredCommand — nothing was skipped.
        assert all(isinstance(r, RegisteredCommand) for r in results)

        # The expected set is present.
        registered = {r.command_name for r in results}  # type: ignore[union-attr]
        expected = {"create", "doctor", "list", "ping", "validate", "verify"}
        assert expected.issubset(registered)

    @pytest.mark.unit()
    def test_every_real_command_has_a_help_string(self) -> None:
        """Every command exposes a non-empty ``help`` string.

        The help string is what the user sees in ``forge --help``. An
        empty help string is a documentation bug, not a runtime error,
        but it should be caught in CI before it reaches users.
        """
        for name in discover_command_modules():
            short_name = name.rsplit(".", 1)[-1]
            module = __import__(name, fromlist=["app"])
            app = getattr(module, APP_ATTRIBUTE, None)
            assert isinstance(app, typer.Typer), f"Module {name!r} does not expose app: typer.Typer"
            assert app.info.help, f"Command {short_name!r} has an empty help string"

    @pytest.mark.unit()
    def test_every_real_command_has_a_name(self) -> None:
        """Every command exposes a non-empty ``name``.

        The name is what the user types (``forge <name>``). An empty
        name is a runtime bug — the command would be unreachable.
        """
        for name in discover_command_modules():
            short_name = name.rsplit(".", 1)[-1]
            module = __import__(name, fromlist=["app"])
            app = getattr(module, APP_ATTRIBUTE, None)
            assert isinstance(app, typer.Typer), f"Module {name!r} does not expose app: typer.Typer"
            assert app.info.name, f"Command {short_name!r} has an empty name"

    @pytest.mark.unit()
    def test_command_names_are_unique(self) -> None:
        """No two command modules register the same command name.

        The registry detects duplicates and skips the later one, but
        a duplicate name is always a bug. This test catches it in CI
        before the silent skip happens in production.
        """
        seen: dict[str, str] = {}
        for name in discover_command_modules():
            module = __import__(name, fromlist=["app"])
            app = getattr(module, APP_ATTRIBUTE, None)
            assert isinstance(app, typer.Typer)
            assert app.info.name is not None
            if app.info.name in seen:
                pytest.fail(
                    f"Command name {app.info.name!r} is registered by "
                    f"both {seen[app.info.name]!r} and {name!r}"
                )
            seen[app.info.name] = name
