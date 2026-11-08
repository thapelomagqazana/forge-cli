"""Unit tests for ``forge.cli.registry``.

The registry is the single place that knows *how* to find commands. Its
logic is non-trivial (discovery, contract validation, graceful failure,
strict mode, duplicate detection), so it deserves its own test file.

Since 1.1.6, it is also the public seam the interactive ``create``
wizard uses to enumerate and load blueprints. Those two functions —
``list_blueprints()`` and ``get_blueprint()`` — are tested here in their
own classes (``TestListBlueprints``, ``TestGetBlueprint``) because they
have their own contracts: sorted output, empty-safe behavior, domain
errors for unknown IDs, and a hint that is actionable rather than a
dead end.

These tests are deliberately decoupled from the real command modules.
They use ``mock.patch`` to install fake modules in ``sys.modules``, so
they can simulate any combination of well-formed and malformed modules
without depending on which real commands currently exist. Adding a new
command does not require touching this file.

Blueprint access and the service seam
-------------------------------------
The registry delegates blueprint access to
``forge.discovery.BlueprintDiscoveryService`` through
``_make_discovery_service``. That function is the *only* seam a test
should substitute — the service's own behaviour (candidate streaming,
precedence, deduplication warnings) is tested in
``tests/unit/discovery/``. What the tests below assert is the
*delegation contract*:

* The registry calls the seam on every invocation (no caching).
* The registry translates the service's ``DiscoveryError`` into the
  user-facing ``BlueprintError`` with an "available blueprints" hint.
* The registry materializes ``variables`` into an immutable tuple so
  the returned entry is safe to pass across threads and store in
  fixtures.
* The hint is built only on the failure path — a successful load
  pays no cost for the message it will not display.

Test organisation
-----------------
- ``TestDiscovery``: module discovery and package validation.
- ``TestRegistration``: happy path — real and fake modules register.
- ``TestGracefulFailure``: malformed modules are skipped, not raised.
- ``TestStrictMode``: malformed modules raise under ``strict=True``.
- ``TestDuplicateDetection``: duplicate command names are detected.
- ``TestOnSkipCallback``: the ``on_skip`` callback is invoked.
- ``TestRealCommands``: every real command satisfies the contract.
- ``TestListBlueprints``: the sorted, empty-safe blueprint enumeration.
- ``TestGetBlueprint``: the single-blueprint view, including the
  domain-error and hint contracts.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
import typer

from forge.cli.registry import (
    APP_ATTRIBUTE,
    COMMANDS_PACKAGE,
    BlueprintEntry,
    RegisteredCommand,
    SkippedCommand,
    discover_command_modules,
    register_commands,
)
from forge.core.exceptions import BlueprintError, DiscoveryError, ExitCode


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

    Used for its *side effect* (patching ``discover_command_modules``
    to return an empty list). Tests that use it must declare it via
    ``@pytest.mark.usefixtures("_empty_discovery")`` rather than as a
    parameter, so it is clear they need the side effect, not a return
    value (``PT019``).
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
        """The real package contains the expected command modules."""
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
    def test_skips_module_without_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    def test_skips_module_with_wrong_app_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    def test_strict_raises_on_missing_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        """In strict mode, the real command modules all pass."""
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
    def test_skips_duplicate_command_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    def test_strict_raises_on_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    def test_on_skip_is_invoked_per_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        """Every module in ``forge.cli.commands`` follows the contract."""
        root = typer.Typer()
        results = register_commands(root, strict=True)

        assert all(isinstance(r, RegisteredCommand) for r in results)

        registered = {r.command_name for r in results}  # type: ignore[union-attr]
        expected = {"create", "doctor", "list", "ping", "validate", "verify"}
        assert expected.issubset(registered)

    @pytest.mark.unit()
    def test_every_real_command_has_a_help_string(self) -> None:
        """Every command exposes a non-empty ``help`` string."""
        for name in discover_command_modules():
            short_name = name.rsplit(".", 1)[-1]
            module = __import__(name, fromlist=["app"])
            app = getattr(module, APP_ATTRIBUTE, None)
            assert isinstance(app, typer.Typer), f"Module {name!r} does not expose app: typer.Typer"
            assert app.info.help, f"Command {short_name!r} has an empty help string"

    @pytest.mark.unit()
    def test_every_real_command_has_a_name(self) -> None:
        """Every command exposes a non-empty ``name``."""
        for name in discover_command_modules():
            short_name = name.rsplit(".", 1)[-1]
            module = __import__(name, fromlist=["app"])
            app = getattr(module, APP_ATTRIBUTE, None)
            assert isinstance(app, typer.Typer), f"Module {name!r} does not expose app: typer.Typer"
            assert app.info.name, f"Command {short_name!r} has an empty name"

    @pytest.mark.unit()
    def test_command_names_are_unique(self) -> None:
        """No two command modules register the same command name."""
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


# -----------------------------------------------------------------------------
# Blueprint enumeration — via the discovery service seam
# -----------------------------------------------------------------------------
# ``list_blueprints()`` and ``get_blueprint()`` delegate to
# ``BlueprintDiscoveryService`` through the module-level
# ``_make_discovery_service`` seam. Tests substitute *that seam* — not
# the service's internals — so the delegation path itself is
# exercised: a refactor that, say, calls a different method on the
# service is caught here, because the fake service is a real object
# obeying the service's protocol, not a mock of one method.
#
# The service's own behaviour (candidate streaming, precedence,
# malformed-skip) is tested in ``tests/unit/discovery/``. Duplicating
# those tests here would create two suites that fail together when a
# discovery rule changes. What these tests prove is narrower and
# complementary: that the registry translates the service's contract
# into the registry's contract without adding or dropping anything.
# -----------------------------------------------------------------------------
class _FakeService:
    """Minimal stand-in for ``BlueprintDiscoveryService``.

    Implements only the two methods the registry calls — ``discover``
    and ``load`` — with the signatures the real service exposes. The
    fake is a real object (not a ``Mock``) so the tests exercise
    attribute access and return-value shapes, which is what would
    break first if the registry started calling a different method or
    assumed a different return type.

    The ``calls`` list lets tests assert on *which* methods were
    called and how many times, which is how the "no caching" contract
    is verified.
    """

    def __init__(
        self,
        *,
        catalogue: list[Any] | None = None,
        loadable: dict[str, Any] | None = None,
        load_raises: Exception | None = None,
    ) -> None:
        self._catalogue = list(catalogue or [])
        self._loadable = dict(loadable or {})
        self._load_raises = load_raises
        # Public so tests can inspect the call log without reaching
        # into a private attribute. The list is the seam's contract,
        # not its implementation detail.
        self.discover_calls: list[int] = []
        self.load_calls: list[str] = []

    def discover(self) -> list[Any]:
        self.discover_calls.append(1)
        return list(self._catalogue)

    def load(self, blueprint_id: str) -> Any:
        self.load_calls.append(blueprint_id)
        if blueprint_id in self._loadable:
            return self._loadable[blueprint_id]
        # Mirror the real service: raise ``DiscoveryError`` when the id
        # is not loadable. The registry is expected to translate this
        # to ``BlueprintError``; a test that substituted the service
        # to raise a different exception would be testing the wrong
        # thing.
        if self._load_raises is not None:
            raise self._load_raises
        raise DiscoveryError(f"Blueprint '{blueprint_id}' not found.")


def _install_service(
    monkeypatch: pytest.MonkeyPatch,
    service: _FakeService,
) -> _FakeService:
    """Install a fake service behind the registry's seam.

    The seam is ``_make_discovery_service`` — a function that returns a
    fresh service per call. Substituting it (rather than caching a
    single instance) preserves the "no caching" semantics of the real
    implementation, which is exactly what the "delegates on every
    call" test exercises.
    """
    monkeypatch.setattr(
        "forge.cli.registry._make_discovery_service",
        lambda: service,
    )
    return service


class _FakeView:
    """Minimal stand-in for ``DiscoveredBlueprint``.

    The registry only touches ``id`` when building the
    available-blueprints hint, so the fake exposes exactly that
    attribute. Using a real object here (not a Mock) makes an
    accidental ``getattr(view, "name")`` in the registry code fail
    loudly, which is the intent: the test is asserting that the
    registry only depends on the fields it *needs*.
    """

    def __init__(self, blueprint_id: str) -> None:
        self.id = blueprint_id


class _FakeBlueprint:
    """Minimal stand-in for a loaded ``Blueprint``.

    The registry's ``get_blueprint`` touches ``variables``, ``name``,
    and ``id``. Everything else is the service's concern. The fake
    exposes exactly those three fields so a future field access in
    the registry is caught by the tests rather than by end users.
    """

    def __init__(
        self,
        *,
        blueprint_id: str,
        name: str = "",
        variables: list[Any] | None = None,
    ) -> None:
        self.id = blueprint_id
        self.name = name
        self.variables = variables if variables is not None else []


class TestListBlueprints:
    """Tests for ``list_blueprints()``."""

    @pytest.mark.unit()
    def test_returns_sorted_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """IDs are sorted so the wizard menu is stable across runs.

        The service guarantees sorted output (ADR-0009); the registry
        re-sorts defensively because the CLI's output shape is a
        public contract. The test asserts the registry's contract, not
        the service's — a service refactor that drops its sort would
        not break the wizard, which is the point of the defensive
        re-sort.
        """
        service = _FakeService(
            catalogue=[
                _FakeView("zzz.last"),
                _FakeView("python.fastapi.api"),
                _FakeView("aaa.first"),
            ]
        )
        _install_service(monkeypatch, service)

        from forge.cli.registry import list_blueprints

        assert list_blueprints() == [
            "aaa.first",
            "python.fastapi.api",
            "zzz.last",
        ]

    @pytest.mark.unit()
    def test_returns_empty_list_for_empty_catalogue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty catalogue returns ``[]``, not an exception.

        The wizard treats an empty list as a ``UserInputError`` with a
        clear hint ("no blueprints installed"). Making
        ``list_blueprints`` raise would force the wizard to catch an
        exception for a normal (if misconfigured) state — and would
        shift the error message from a domain error to whatever the
        service happened to raise.
        """
        _install_service(monkeypatch, _FakeService(catalogue=[]))

        from forge.cli.registry import list_blueprints

        assert list_blueprints() == []

    @pytest.mark.unit()
    def test_delegates_to_the_service_seam_every_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each call goes through the service — no caching in the registry.

        The service deliberately re-resolves on every call so user
        edits are picked up immediately (ADR-0009's cache policy).
        A second cache in the registry would create two sources of
        truth and a cache-invalidation bug waiting to happen. This
        test proves the registry is a facade, not a cache: two calls
        mean two delegations.
        """
        service = _FakeService(catalogue=[_FakeView("python.fastapi.api")])
        _install_service(monkeypatch, service)

        from forge.cli.registry import list_blueprints

        list_blueprints()
        list_blueprints()

        assert len(service.discover_calls) == 2, (
            "list_blueprints() must call the service on every "
            "invocation; if this fails, someone added a "
            "registry-level cache and created a second source of "
            "truth."
        )


class TestGetBlueprint:
    """Tests for ``get_blueprint(id)``."""

    @pytest.mark.unit()
    def test_returns_entry_with_all_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The returned ``BlueprintEntry`` carries id, display_name, variables."""
        variables = [object(), object()]
        service = _FakeService(
            loadable={
                "python.fastapi.api": _FakeBlueprint(
                    blueprint_id="python.fastapi.api",
                    name="Python FastAPI API",
                    variables=variables,
                ),
            }
        )
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        entry = get_blueprint("python.fastapi.api")

        assert isinstance(entry, BlueprintEntry)
        assert entry.id == "python.fastapi.api"
        assert entry.display_name == "Python FastAPI API"
        assert entry.variables == tuple(variables)

    @pytest.mark.unit()
    def test_falls_back_to_id_when_name_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A blueprint without a display name uses its ID as the label.

        The wizard menu must always have a label; making the fallback
        the registry's job (not the wizard's) keeps the wizard free of
        per-blueprint special cases. The fallback fires when ``name``
        is empty *or* missing — both are equivalent from the user's
        perspective ("no label was provided").
        """
        service = _FakeService(
            loadable={
                "typescript.react.app": _FakeBlueprint(
                    blueprint_id="typescript.react.app",
                    name="",
                ),
            }
        )
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        entry = get_blueprint("typescript.react.app")
        assert entry.display_name == "typescript.react.app"

    @pytest.mark.unit()
    def test_materializes_variables_to_a_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``variables`` is a tuple, not the model's list.

        The returned ``BlueprintEntry`` is frozen; a mutable list
        inside it would be a footgun (hash breaks, downstream handler
        can mutate). Materializing to a tuple at the boundary makes
        the entry safe to pass across threads and store in fixtures.
        """
        service = _FakeService(
            loadable={
                "python.fastapi.api": _FakeBlueprint(
                    blueprint_id="python.fastapi.api",
                    variables=["v1", "v2"],
                ),
            }
        )
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        entry = get_blueprint("python.fastapi.api")
        assert isinstance(entry.variables, tuple)
        assert entry.variables == ("v1", "v2")

    @pytest.mark.unit()
    def test_unknown_id_raises_blueprint_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unknown IDs raise ``BlueprintError``, not ``DiscoveryError``.

        The exit code is the contract: the service raises
        ``DiscoveryError`` (also exit 2), but the registry *translates*
        it to ``BlueprintError`` so callers see the domain error type
        the CLI handler is written against. The translation is what
        this test locks in — a future refactor that "simplifies" by
        removing the try/except would surface ``DiscoveryError`` to
        the handler, which dispatches on ``ForgeError`` and would
        still work, but the *message* would lose the "available
        blueprints" hint built from the catalogue. Both matter.
        """
        service = _FakeService(loadable={})
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        with pytest.raises(BlueprintError) as info:
            get_blueprint("does.not.exist")

        assert info.value.exit_code == ExitCode.BLUEPRINT_ERROR
        assert info.value.exit_code == 2
        assert "does.not.exist" in str(info.value)

    @pytest.mark.unit()
    def test_unknown_id_hint_lists_available_blueprints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hint lists what *is* available — a dead end otherwise.

        A hint that says "not found" without alternatives sends the
        user to ``forge list`` (if that command exists) or back to
        the docs. The hint must name the options inline so the error
        is actionable. The registry builds the list from the same
        ``discover()`` call the wizard uses, so the hint and the menu
        agree by construction.
        """
        service = _FakeService(
            catalogue=[
                _FakeView("python.fastapi.api"),
                _FakeView("typescript.react.app"),
            ],
            loadable={},
        )
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        with pytest.raises(BlueprintError) as info:
            get_blueprint("does.not.exist")

        assert info.value.hint is not None
        assert "python.fastapi.api" in info.value.hint
        assert "typescript.react.app" in info.value.hint

    @pytest.mark.unit()
    def test_unknown_id_hint_handles_empty_catalogue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no blueprints installed, the hint says so explicitly.

        ``"Available blueprints: ."`` (with nothing after the colon)
        is a dead end dressed as a hint. The empty case must say
        "(none installed)" or similar so the user knows the problem
        is the installation, not their typo.
        """
        service = _FakeService(catalogue=[], loadable={})
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        with pytest.raises(BlueprintError) as info:
            get_blueprint("anything")

        assert info.value.hint is not None
        assert "none" in info.value.hint.lower()

    @pytest.mark.unit()
    def test_known_id_does_not_consult_available_for_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hint is not constructed on the success path.

        A small but real bug class: building the "available
        blueprints" string eagerly (before the lookup) would be
        wasted work on every successful load, and would also mean a
        successful load pays for a ``discover()`` call that is only
        useful on failure. The test asserts the service's
        ``discover`` was *not* called for a successful ``load`` —
        which is only possible if the hint construction is inside the
        failure branch.
        """
        service = _FakeService(
            loadable={
                "python.fastapi.api": _FakeBlueprint(
                    blueprint_id="python.fastapi.api",
                    name="FastAPI",
                ),
            }
        )
        _install_service(monkeypatch, service)

        from forge.cli.registry import get_blueprint

        entry = get_blueprint("python.fastapi.api")

        assert isinstance(entry, BlueprintEntry)
        # The success path must not consult the catalogue. If this
        # assertion fails, the hint is being built eagerly.
        assert service.discover_calls == [], (
            "get_blueprint() must not call discover() on the success "
            "path; the available-blueprints hint is only needed when "
            "the lookup fails."
        )
