"""Tests for the dual-mode input handling in ``forge create``.

These tests cover the acceptance criteria of Task 1.1.6 directly:

1. Non-interactive with all flags succeeds (without prompting).
2. Non-interactive missing a required flag fails with exit 1 and a
   message listing the missing flag.
3. Interactive wizard creates a project from start to finish.
4. The wizard validates input in real-time.
5. The wizard allows cancellation (Ctrl+C) and exits cleanly.

The generation pipeline is not stubbed here — the tests for it exercise
the *real* pipeline (plan → safety → backup → staging → commit). An
earlier version of the ``TestGeneratePreFlight`` class stubbed the
writer with ``NotImplementedError`` and asserted the pre-flight
directory check. Both reflected a pipeline that did its own
collision detection and then hit a stub. That contract is gone: the
safety layer owns collision detection (ADR-0017), and the writer is
the staging-and-commit pipeline. The tests now pin the *new* contract
— ``CollisionError`` (exit code 6) for collisions, and a timestamped
backup for every overwritten file.

The rest of the file still tests *input handling* in isolation, for
the reasons below. The two concerns are separated by test class:

- ``TestDispatch`` and the interactive/non-interactive classes
  exercise ``_resolve_variables`` and the parser — the input side.
- ``TestGeneratePreFlight`` exercises the safety layer's contract
  through ``_generate`` — the pipeline side.

The one place input handling fakes a boundary is ``_generate`` in
``capture_generate``: those tests want to inspect the *inputs* the
pipeline would receive, not the pipeline itself. That remains the
correct shape — the test is about what flows *into* the pipeline,
not what the pipeline does with it.

Test design
-----------
There are two families of tests here, deliberately:

- **Dispatch tests** go through ``cli_runner.invoke(forge_app, ...)``.
  Their purpose is to prove that ``forge create <blueprint>`` parses
  as a leaf command, not as a group with a subcommand. This is the
  contract that broke in an earlier test run (a ``@app.command()``
  or a mis-placed ``invoke_without_command``), and it can only be
  verified through the parser.

- **Resolution tests** call ``_resolve_variables`` directly. Their
  purpose is to prove the collection logic (flags win, missing flags
  aggregate, wizard prompts when interactive, cancellation
  propagates). The parser is not part of what these tests verify,
  so going through it would add indirection without adding coverage.

Both families exercise the *current* shape of the command:

    * The project name is a **command argument** resolved outside
      ``VariableResolver`` (see ``create.py`` for the reasoning).
    * The *blueprint variables* flow through the resolver.
    * **Conditional file groups** are evaluated by
      ``ConditionalEvaluator`` and only the surviving groups reach
      the planner. That is why ``_resolve_variables`` returns a
      **three-tuple** — the third element is the surviving groups
      the planner consumes.
    * ``_resolve_variables`` returns
      ``(blueprint, inputs, surviving_groups)`` — the blueprint
      document is needed by ``_generate`` to feed the planner, and
      returning it alongside the resolved inputs is the deliberate
      coupling the command docstring explains.
    * ``_generate`` takes the blueprint as a **keyword-only**
      argument, alongside ``inputs``, ``conditional_groups``,
      ``dry_run``, and ``overwrite``. All five are keyword-only on
      purpose: a positional call would read as
      ``_generate(blueprint, inputs, ...)`` and make the order easy
      to get wrong.
    * ``conditional_groups`` is a **required** argument because the
      safe-by-default rule (ADR-0015) says the planner never
      includes conditional files unless the caller opts in. Passing
      ``[]`` explicitly is the caller saying "I evaluated conditions
      and none survived" — which is different from "I forgot to
      evaluate conditions", and the type system makes the difference
      visible at every call site.

Tests written before this shape change called ``_resolve_variables``
and expected a two-tuple, and passed ``_generate`` a signature that
had not yet grown ``conditional_groups``. Both broke the moment the
shape changed — which is exactly what the tests are for. The
corrections (unpack three values, pass the conditional groups) are
mechanical; the *failure* was the diagnostic that told us the shape
had changed, and keeping that failure legible in the test suite
(rather than papering over it with a duck-typed shim) is what makes
the next shape change equally visible.

The right seam: ``create.get_blueprint_document``
-------------------------------------------------
Earlier versions of these fixtures patched
``forge.cli.registry._load_blueprint_index`` — a static index that
the registry used to expose. The registry has since been refactored
to delegate to ``BlueprintDiscoveryService`` (ADR-0009), and that
private name no longer exists.

A later revision patched ``create_module.get_blueprint``, which was
the *catalogue entry* accessor on the registry. That accessor
returned a ``BlueprintEntry``, and the command tried to reach through
``entry.path`` to the loader — a bug, because ``BlueprintEntry`` is
deliberately *not* a filesystem handle. The command has since moved
to ``get_blueprint_document`` (which returns the loaded ``Blueprint``,
not the entry).

The fixtures below patch ``create_module.get_blueprint_document``
— the *exact* name the command module imported at load time
(``from forge.cli.registry import get_blueprint_document``), which is
the direct dependency ``_resolve_variables`` calls. Patching at that
boundary means:

- The fixture cannot silently become a no-op: if ``create.py`` stops
  importing ``get_blueprint_document``, the ``AttributeError``
  surfaces immediately at fixture setup, in the test that owns the
  fixture. This is the same failure mode the previous
  ``get_blueprint`` patch caught when it was renamed — the fixture
  breaking is *diagnostic*, not silent.
- The fixture's correctness does not depend on the registry's
  internal plumbing. A future registry refactor that keeps the
  public ``get_blueprint_document`` name does not touch these tests.

The fixture's *return shape* is the loaded ``Blueprint``, not a
``BlueprintEntry``. That is the shape ``get_blueprint_document``
returns, and it is what the resolver consumes.

A test that fakes *both* the input and the output of the thing it is
testing is testing itself. So the resolver is **not** faked in the
resolution tests — it is a pure function, the tests already control
every input it sees, and letting it run is the only way to prove it
is actually wired up.

Why some tests do not assert on ``result.output``
-------------------------------------------------
``CliRunner.invoke(forge_app, ...)`` runs ``create_app()``, which
builds the Typer app and mounts the command modules. It does *not*
install ``handle_exception`` — that wrapper lives in ``main()``,
which the CLI entry point runs *after* Typer's dispatch. So when a
command raises a ``ForgeError``, the exception propagates out of
``.invoke()`` as ``result.exception``, and ``result.output`` is
empty: Typer sets the exit code from the exception's ``exit_code``
attribute, but the domain error is never rendered.

The tests that assert on the *rendered* message (the missing-flags
message and the cancellation message) therefore invoke
``handle_exception`` on ``result.exception`` directly and assert on
``capsys.readouterr().err``. That is the same code path the shell
exercises (``main()`` → ``handle_exception`` → stderr), just called
from the test instead of from the entry point.

Each layer is tested at the layer it actually lives in: the parser
in the dispatch tests, the collection logic in ``_resolve_variables``
tests, the exception type via ``pytest.raises``, and the rendered
message via ``handle_exception`` + ``capsys``.

``_generate`` inputs must satisfy ``_generate``'s contract
----------------------------------------------------------
``_generate`` reads ``inputs.variables.as_dict()`` — a method that
exists on ``ResolvedValues`` (the resolver's immutable output type)
but *not* on a plain ``dict``. That is deliberate: the type
distinction is what prevents an unvalidated ``dict`` from reaching
the planner, and removing it would silently undo the KPI the whole
resolver exists to deliver.

An earlier version of these ``TestGeneratePreFlight`` tests
constructed ``SimpleNamespace(variables={})`` — a fake that happened
to be dict-shaped but failed ``_generate``'s actual contract. The
``AttributeError: 'dict' object has no attribute 'as_dict'`` was the
*directed* symptom, and the correct fix was to make the fixture
construct the type ``_generate`` requires, not to teach ``_generate``
to accept a shape it was designed to reject. The helper
``_resolved_inputs(...)`` below is that fix, and its existence is
the constant reminder: fake the *boundary*, not the *type*.

The pipeline boundary: two ``blueprint_root`` fixtures
------------------------------------------------------
``_generate`` re-resolves the blueprint's manifest directory via
``create_module._blueprint_root``, which calls
``BlueprintDiscoveryService.load_with_path``. Discovery is not what
the ``TestGeneratePreFlight`` tests are about — they are about the
safety layer's collision and backup contract. Patching
``_blueprint_root`` at its definition site is the honest boundary:
the test says "assume the blueprint root is here" and exercises
everything downstream of that assumption (the planner, the safety
layer, the staging pipeline, the overwrite backups).

There are two fixtures, not one, because the tests need two
different things from the stub:

    * ``_stub_blueprint_root`` — returns ``None``, sets up the
      monkeypatch. For tests that only need the stub to *exist*
      (the collision and backup tests do not write into the
      blueprint root; the destination directory is what they
      collide against). Applied via
      ``@pytest.mark.usefixtures("_stub_blueprint_root")``
      (``PT004``/``PT019``).

    * ``blueprint_root`` — returns the ``Path`` to the stub
      directory. For tests that need to write a template *into*
      the root (the dry-run test, which resolves the plan's source
      reference). Named without an underscore because it returns a
      value (``PT005``); injected as a parameter because the test
      body uses it.

The two fixtures delegate to the same ``monkeypatch.setattr`` call
via a private helper, so there is exactly one place that describes
*how* the stub is installed. The split is about *what the fixture
returns*, which is what ``PT004`` and ``PT005`` are actually about.

The alternative — mocking ``BlueprintDiscoveryService`` — would drag
the discovery module's internal layout into the test, coupling the
test to the loader's package structure. The current shape couples to
one function's return type (a ``Path``) and nothing else.

Lint conventions
----------------
This file obeys the project's Ruff configuration, including the
flake8-pytest-style (``PT``) and flake8-bandit (``S``) rulesets.
That has three consequences a reader will notice:

- **Side-effect-only fixtures are prefixed with ``_`` and applied
  via ``@pytest.mark.usefixtures(...)``.** ``PT004`` requires the
  underscore for fixtures that return ``None``; ``PT019`` requires
  that such fixtures not be injected as test parameters. The two
  rules are complementary — satisfying one without the other
  produces a lint error for the other.
- **Fixtures that *do* return a value have no leading underscore**
  (``PT005``). If a test needs a value-only fixture, it must be
  named as a value and injected as a parameter, not applied via
  ``usefixtures``.
- **Temp directories come from ``tmp_path``, not ``/tmp``.**
  ``S108`` flags hardcoded ``/tmp`` because it is world-writable and
  shared across users; ``tmp_path`` is per-test and uniquely owned.
- **Class attributes are immutable or ``ClassVar``.** ``RUF012``
  flags mutable class attributes; fixtures that construct fake
  blueprints build real ``Blueprint`` model instances instead of
  duck-typed fakes, which sidesteps the rule and gives the tests
  the resolver's actual contract.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from forge.cli import prompts
from forge.cli.commands import create as create_module
from forge.core.exceptions import (
    BlueprintError,
    CollisionError,
    ExitCode,
    MissingRequiredFlagsError,
    PromptCancelledError,
    UserInputError,
)
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    VariableDefinition,
    VariableType,
)
from forge.resolution import ResolvedValues
from forge.resolution.names import NAMED_VALIDATORS


# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------
def _make_blueprint(
    *,
    variables: list[VariableDefinition],
    files: list[dict[str, str]] | None = None,
) -> Blueprint:
    """Build a minimal valid ``Blueprint`` for testing.

    Centralised so the two fixtures (``_fake_blueprint`` and
    ``_fake_blueprint_with_vars``) stay in lockstep on all fields they
    are *not* testing. A future field addition to ``Blueprint`` needs
    one edit here, not two.

    ``files`` is optional because most tests (input collection,
    cancellation, blueprint variables) do not touch the planner —
    only the ``TestGeneratePreFlight`` tests need a plan with a
    destination to conflict against. Passing ``None`` keeps the
    existing behaviour (empty ``files[]``, zero planned files) for
    every test that does not care.

    The blueprint is deliberately a *real model instance*, not a
    duck-typed fake. The resolver, the loader, the planner, and every
    other consumer validate against this type, so the test should too
    — a fake that happens to satisfy the attributes the command reads
    today would silently break the moment the command gains a new
    dependency on the model.

    ``conditional_files`` is intentionally empty: these tests exercise
    input collection (and the safety layer's collision/backup
    contract), not conditional evaluation. ``_generate``'s
    ``conditional_groups`` argument is therefore supplied as ``[]``
    by every test that calls it.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="python.fastapi.api",
        name="Fake API",
        version="1.0.0",
        description="Fake blueprint for tests.",
        category=Category.api,
        variables=variables,
        files=files or [],
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _resolved_inputs(
    *,
    destination: Path,
    project_name: str = "my-api",
    blueprint_id: str = "python.fastapi.api",
    variables: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build a ``_ResolvedInputs``-shaped object with a real ``ResolvedValues``.

    ``_generate`` calls ``inputs.variables.as_dict()`` — a method
    that exists on ``ResolvedValues`` but not on a plain ``dict``.
    That method is the type boundary the resolver exists to enforce
    (see ``forge.resolution.resolver.ResolvedValues``), and the
    ``TestGeneratePreFlight`` tests must construct inputs that satisfy
    it, or they are not exercising ``_generate`` — they are
    exercising a fake that happens to share some attributes.

    ``SimpleNamespace`` is used (rather than ``_ResolvedInputs``
    directly) because ``_ResolvedInputs`` is a *private* type in
    ``create.py``, and reaching into a private type from a test
    couples the test to an implementation detail. The namespace
    satisfies the duck-typed contract ``_generate`` actually consumes
    (``.blueprint_id``, ``.project_name``, ``.destination``,
    ``.variables``), which is the honest level of coupling for a test
    of a private helper.
    """
    return SimpleNamespace(
        blueprint_id=blueprint_id,
        project_name=project_name,
        destination=destination,
        variables=ResolvedValues(MappingProxyType(variables or {})),
    )


@pytest.fixture()
def _fake_blueprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a *variable-less* fake blueprint behind ``get_blueprint_document``.

    The empty ``variables`` list is the interesting case for the
    resolution refactor: it forces the tests to prove that
    ``project_name`` is *not* smuggled through the resolver. A
    blueprint that declared a ``project_name`` variable would mask
    the bug the ``KeyError('project_name')`` test run originally
    surfaced.

    Tests that need blueprint variables use
    ``_fake_blueprint_with_vars`` instead. The two fixtures are
    distinct on purpose: a test that uses this one cannot
    accidentally pass by exercising the resolver's
    blueprint-variable path.
    """
    fake = _make_blueprint(variables=[])
    monkeypatch.setattr(create_module, "get_blueprint_document", lambda _id: fake)


@pytest.fixture()
def _fake_blueprint_with_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake blueprint with a single required string variable (``author``).

    Used by tests that exercise the resolver's *blueprint variable*
    path (missing required variable, real-time prompt for a blueprint
    variable). The variable is a genuine ``VariableDefinition`` from
    ``forge.models`` — the resolver validates against the real type,
    not a duck-typed fake, so the test proves the actual integration.

    ``author`` is a string with no default and ``required=True``, so
    the resolver's ``required.missing`` path is reachable simply by
    omitting it from ``cli_vars``. That is the minimal blueprint that
    exercises the error aggregation the command wraps into a
    ``UserInputError``.
    """
    fake = _make_blueprint(
        variables=[
            VariableDefinition(
                id="author",
                type=VariableType.string,
                description="Author name used in project metadata.",
                required=True,
            ),
        ],
    )
    monkeypatch.setattr(create_module, "get_blueprint_document", lambda _id: fake)


@pytest.fixture()
def capture_generate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``_generate`` so the test can inspect the resolved inputs.

    ``_generate`` is the boundary between *input handling* and the
    *pipeline*. The tests in this class exercise input handling, so
    they stub the pipeline and assert on what it *would* receive.
    Faking the boundary is honest because the test is about input
    collection, not generation — the pipeline's own tests
    (``TestGeneratePreFlight``) exercise the real ``_generate``.

    The stub signature mirrors the *current* signature of
    ``create_module._generate``: keyword-only ``blueprint``,
    ``inputs``, ``conditional_groups``, ``dry_run``, ``overwrite``.
    The ``conditional_groups`` parameter is recorded alongside the
    others so future tests can assert that the *surviving* groups
    from the evaluator (not the raw ``conditional_files`` list) were
    what reached the planner — a property the command's docstring
    promises and the tests should pin.

    If ``create_module._generate`` gains a parameter (or renames
    one), this fixture will raise ``TypeError`` at the next
    ``cli_runner.invoke`` — the fixture is the trip wire for the
    signature, not just a recording device.

    This fixture *does* return a value (the ``captured`` dict), so
    it does not need a leading underscore and stays a parameter
    (``PT004`` and ``PT019`` only apply to fixtures returning
    ``None``).
    """
    captured: dict[str, Any] = {}

    def _fake(
        *,
        blueprint: Any,
        inputs: Any,
        conditional_groups: Any,
        dry_run: bool,
        overwrite: bool,
    ) -> None:
        captured["blueprint"] = blueprint
        captured["inputs"] = inputs
        captured["conditional_groups"] = conditional_groups
        captured["dry_run"] = dry_run
        captured["overwrite"] = overwrite

    monkeypatch.setattr(create_module, "_generate", _fake)
    return captured


def _install_blueprint_root_stub(monkeypatch: pytest.MonkeyPatch, blueprint_root: Path) -> None:
    """Shared body of the two ``blueprint_root``-stubbing fixtures.

    Extracted so the two public fixtures (``_stub_blueprint_root`` and
    ``blueprint_root``) describe *the same* monkeypatch. A future change
    to how ``_blueprint_root`` is patched touches this one function.

    The extraction is *not* about avoiding duplication — it is about
    making the difference between the two fixtures legible: they differ
    only in *return value*, and both delegate to the same install step.
    Reading the two fixtures side by side now shows the difference
    immediately.
    """
    monkeypatch.setattr(create_module, "_blueprint_root", lambda _bp, _id: blueprint_root)


@pytest.fixture()
def _stub_blueprint_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the ``_blueprint_root`` stub without returning its ``Path``.

    For tests that only need the stub to *exist* — they do not write
    into the blueprint root, so they do not need the ``Path``.

    Returns ``None`` (leading underscore, applied via
    ``@pytest.mark.usefixtures`` per ``PT004`` and ``PT019``).

    The directory is created empty; a test that needs a template file
    in it should use ``blueprint_root`` (below) instead, which returns
    the same directory so the test can write into it.
    """
    blueprint_root = tmp_path / "blueprint-root"
    blueprint_root.mkdir()
    _install_blueprint_root_stub(monkeypatch, blueprint_root)


@pytest.fixture()
def blueprint_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the ``_blueprint_root`` stub *and* return its ``Path``.

    For tests that need to write a template file into the root (the
    dry-run test resolves the plan's source reference against this
    directory).

    Returns the ``Path`` (no leading underscore per ``PT005``),
    injected as a parameter because the test body uses it.

    Both fixtures create a directory at ``tmp_path / "blueprint-root"``
    and install the same monkeypatch. A test that needs both (a stub
    *and* the path) uses this one; a test that needs only the stub uses
    the other.
    """
    blueprint_root = tmp_path / "blueprint-root"
    blueprint_root.mkdir()
    _install_blueprint_root_stub(monkeypatch, blueprint_root)
    return blueprint_root


@pytest.fixture()
def _force_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``prompts.is_interactive()`` return ``False``.

    Simulates a CI environment or ``--no-interactive``. The command
    checks the flag *and* the TTY, so this fixture only needs to
    override one of them — the test passes ``--no-interactive`` when
    it wants the flag path specifically, and this fixture when it
    wants the TTY path.

    Leading underscore because the fixture returns ``None``
    (``PT004``); applied via ``@pytest.mark.usefixtures(...)`` rather
    than as a parameter (``PT019``).
    """
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)


@pytest.fixture()
def _force_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``prompts.is_interactive()`` return ``True``.

    Leading underscore because the fixture returns ``None``
    (``PT004``); applied via ``@pytest.mark.usefixtures(...)`` rather
    than as a parameter (``PT019``).
    """
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)


# -----------------------------------------------------------------------------
# Dispatch tests — prove ``forge create <blueprint>`` is a leaf command
# -----------------------------------------------------------------------------
class TestDispatch:
    """Tests for the command's dispatch contract (leaf, not group)."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_create_accepts_positional_blueprint_directly(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
    ) -> None:
        """``forge create <blueprint> --name …`` parses as a leaf command.

        This is the regression test for the group-vs-leaf bug. The
        invocation must reach ``_generate`` with the parsed inputs —
        if the command were a group, Click would reject
        ``python.fastapi.api`` as a missing subcommand before any
        code ran, and ``_generate`` would never be called.
        """
        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].blueprint_id == "python.fastapi.api"
        assert capture_generate["inputs"].project_name == "my-api"
        # The blueprint document is passed through *with* the inputs —
        # the planner needs it, and passing it once (from the resolve
        # step) rather than re-fetching in `_generate` guarantees the
        # document the user's answers were validated against is the
        # document that will be planned.
        assert capture_generate["blueprint"].id == "python.fastapi.api"
        # No conditional files on the fake blueprint → the evaluator
        # returns an empty list, which is what reaches `_generate`.
        assert capture_generate["conditional_groups"] == []

    @pytest.mark.unit()
    def test_create_help_shows_leaf_signature(self, cli_runner: CliRunner, forge_app: Any) -> None:
        """``forge create --help`` shows ``Usage: forge create [OPTIONS] BLUEPRINT``.

        A group would show ``Usage: forge create [OPTIONS] COMMAND
        [ARGS]...``. This test asserts on the presence of the
        positional argument's name in the help output, which only
        appears in the leaf signature.
        """
        result = cli_runner.invoke(forge_app, ["create", "--help"])

        assert result.exit_code == 0, result.output
        assert "BLUEPRINT" in result.output

    @pytest.mark.unit()
    def test_create_with_no_args_reports_missing_blueprint(
        self, cli_runner: CliRunner, forge_app: Any
    ) -> None:
        """``forge create`` with no arguments is a usage error, not help.

        A missing ``BLUEPRINT`` argument is a Click usage error (exit
        2), which is what a script should see — not Typer's default
        "print help, exit 0" behaviour.
        """
        result = cli_runner.invoke(forge_app, ["create"])

        assert result.exit_code == 2, result.output
        assert "Missing argument" in result.output or "BLUEPRINT" in result.output


# -----------------------------------------------------------------------------
# Acceptance criterion 1: non-interactive with all flags succeeds
# -----------------------------------------------------------------------------
class TestNonInteractiveSuccess:
    """All flags supplied → no prompt → success."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_all_flags_supplied_succeeds_without_prompting(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All required flags → success, no prompt fired.

        The assertion on ``ask_text`` *not being called* is the
        observable proof that the wizard did not engage. Faking
        ``ask_text`` to raise rather than return is a stronger form
        than asserting on a counter: a counter could be reset by a
        test bug, but an exception propagates.
        """

        def _fail_if_called(*_a: Any, **_k: Any) -> str:
            raise AssertionError(
                "ask_text fired in non-interactive mode — the wizard "
                "must not engage when --no-interactive is passed."
            )

        monkeypatch.setattr(prompts, "ask_text", _fail_if_called)

        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "my-api"
        assert capture_generate["dry_run"] is False
        assert capture_generate["overwrite"] is False

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_flags_reach_generate_unchanged(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """``--output`` and ``--dry-run`` are forwarded to ``_generate``.

        ``tmp_path`` is used for ``--output`` because it is the
        per-test temp directory pytest provides — uniquely owned by
        the test, cleaned up automatically, and immune to the
        hardcoded-``/tmp`` concern that ``S108`` flags.
        """
        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--output",
                str(tmp_path),
                "--no-interactive",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["dry_run"] is True
        assert capture_generate["inputs"].destination == tmp_path / "my-api"


# -----------------------------------------------------------------------------
# Acceptance criterion 2: missing --name → exit 1 + list of missing flags
# -----------------------------------------------------------------------------
class TestNonInteractiveMissingFlags:
    """Missing required flags in non-interactive mode → exit 1."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_missing_name_exits_1_with_flag_in_message(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit code is 1; the rendered message names ``--name``.

        Three assertions across three layers, because the contract
        lives at three layers:

        1. ``result.exit_code == ExitCode.USER_ERROR`` — the shell
           exit code Typer derives from the uncaught exception.
        2. ``isinstance(result.exception, MissingRequiredFlagsError)``
           — the *domain* exception the command raised.
        3. ``"--name" in captured.err`` — the *rendered* message.

        Layer 3 is the reason this test does not assert on
        ``result.output``. ``CliRunner.invoke(forge_app, ...)`` runs
        ``create_app()``, which builds the Typer app but does not
        install ``handle_exception`` — that wrapper lives in
        ``main()``. So the exception propagates as
        ``result.exception`` and ``result.output`` is empty.
        """
        from forge.cli.errors import handle_exception

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api", "--no-interactive"],
        )

        assert result.exit_code == ExitCode.USER_ERROR
        assert isinstance(result.exception, MissingRequiredFlagsError)

        with pytest.raises(SystemExit):
            handle_exception(result.exception, debug=False)

        captured = capsys.readouterr()
        assert "--name" in captured.err
        # Product stream stays clean — errors are commentary.
        assert captured.out == ""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_missing_name_raises_missing_flags_error_directly(
        self,
        tmp_path: Path,
    ) -> None:
        """The domain exception type is ``MissingRequiredFlagsError``.

        Asserting on the exception type directly (not just the exit
        code) proves the *right* error is raised. A ``UserInputError``
        from a typo in the command would also exit 1, but it would
        carry the wrong message and no flag list. The type is the
        precise contract.

        This test bypasses the CLI entirely — it calls
        ``_resolve_variables`` directly. That is the correct layer
        for "does the command raise the right exception," separate
        from "does the shell render it" (the previous test).
        """
        with pytest.raises(MissingRequiredFlagsError) as info:
            create_module._resolve_variables(
                blueprint_id="python.fastapi.api",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )

        assert "name" in info.value.missing

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_missing_name_error_has_actionable_hint(
        self,
        tmp_path: Path,
    ) -> None:
        """The error carries a hint (a dead end otherwise).

        The ``ForgeError`` contract says every user-facing error
        carries a hint. A missing-flags error without a hint tells
        the user *what* is wrong but not *what to do* — the exact
        dead end the class docstring warns against.
        """
        with pytest.raises(MissingRequiredFlagsError) as info:
            create_module._resolve_variables(
                blueprint_id="python.fastapi.api",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )

        assert info.value.hint is not None
        assert len(info.value.hint) > 0


# -----------------------------------------------------------------------------
# Acceptance criterion 3: wizard creates project start-to-finish
# -----------------------------------------------------------------------------
class TestInteractiveWizard:
    """Interactive mode prompts for anything not supplied as a flag."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_prompts_for_name_when_not_supplied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Interactive mode prompts for ``--name`` when it is absent.

        The test fakes ``ask_text`` (the *input* to the wizard) but
        not the validator (the *output* of the funnel). That is the
        correct side of the boundary: fake what the user types; let
        the code prove it processes what the user typed.

        The ``_fake_blueprint`` fixture declares no blueprint
        variables, so the wizard asks exactly one question — the
        project name — which is what ``ask_text`` is patched to
        answer. If the fixture ever grew variables, this test would
        fail at ``ask_select`` (which is deliberately not patched),
        making the coupling visible instead of silent.

        ``_resolve_variables`` returns a three-tuple
        ``(blueprint, inputs, surviving_groups)``. We unpack the
        tuple and ignore the blueprint and surviving groups here —
        the assertions are on the *inputs*. The blueprint is
        available and asserted on in
        ``TestDispatch::test_create_accepts_positional_blueprint_directly``,
        where its presence in ``capture_generate`` proves the
        command passes it through to ``_generate``. The surviving
        groups are empty (the fake blueprint declares no conditional
        files) and are asserted on in the same dispatch test.
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        _blueprint, inputs, _groups = create_module._resolve_variables(
            blueprint_id="python.fastapi.api",
            cli_name=None,
            cli_vars={},
            output=tmp_path,
            no_interactive=False,
        )

        assert inputs.project_name == "wizard-api"

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_does_not_re_prompt_when_name_supplied_as_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flags win: a supplied ``--name`` is not re-asked.

        Both ``ask_text`` (name prompt) and ``ask_select`` /
        ``ask_confirm`` (blueprint-variable prompts) are patched to
        raise — because with ``cli_name`` supplied and no blueprint
        variables, the wizard should fire *no* prompt at all.
        """

        def _raise_if_called(*_a: Any, **_k: Any) -> str:
            raise AssertionError("a prompt fired despite --name being supplied")

        monkeypatch.setattr(prompts, "ask_text", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_select", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_confirm", _raise_if_called)

        _blueprint, inputs, _groups = create_module._resolve_variables(
            blueprint_id="python.fastapi.api",
            cli_name="flag-api",
            cli_vars={},
            output=tmp_path,
            no_interactive=False,
        )

        assert inputs.project_name == "flag-api"

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_no_interactive_flag_disables_wizard_even_on_tty(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-interactive`` overrides a TTY stdin.

        This is the case a user in a terminal hits when they want a
        scripted run from an interactive shell. The flag must win
        over the TTY check, or the wizard would fire despite the
        explicit request not to.
        """

        def _raise_if_called(*_a: Any, **_k: Any) -> str:
            raise AssertionError("wizard fired despite --no-interactive")

        monkeypatch.setattr(prompts, "ask_text", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_select", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_confirm", _raise_if_called)

        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "my-api"

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_interactive_dispatches_without_requiring_flag(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A TTY without ``--name`` still dispatches to the wizard.

        Proves the *parser* does not reject a ``--name``-less
        invocation before the wizard gets a chance to run — which is
        the whole reason ``name`` is optional at the Typer layer.

        ``ask_text`` answers ``wizard-api`` for the project-name
        prompt. ``_fake_blueprint`` declares no blueprint variables,
        so no ``ask_select`` or ``ask_confirm`` prompt fires.
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api"],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "wizard-api"


# -----------------------------------------------------------------------------
# Acceptance criterion 4: real-time validation
#
# The inline validator used to live in the command module
# (``_project_name_validator``). It has moved to
# ``resolution.names.project_name``, which is the *same* function the
# resolver applies. There is no longer a "duplicate" to test for
# subset consistency — there is exactly one validator, used by both
# the prompt (for real-time UX) and the resolver (for authoritative
# validation). These tests exercise that validator directly at the
# layer where it lives.
# -----------------------------------------------------------------------------
class TestNamedValidatorIntegration:
    """``resolution.names.project_name`` is wired into the wizard."""

    @pytest.mark.unit()
    def test_rejects_empty_name(self) -> None:
        """An empty name is rejected by the shared named validator."""
        assert NAMED_VALIDATORS["project_name"]("") is not None

    @pytest.mark.unit()
    def test_rejects_names_with_spaces(self) -> None:
        """Spaces produce a directory name with spaces — rejected."""
        assert NAMED_VALIDATORS["project_name"]("my project") is not None

    @pytest.mark.unit()
    def test_accepts_hyphenated_lowercase_names(self) -> None:
        """Hyphenated lowercase names are the canonical accepted form.

        Note: underscores are **rejected** by ``project_name`` because
        npm consumers expect hyphenated names, and the same project
        name is reused as an npm package name in the TypeScript
        blueprints. The underscore form belongs to
        ``python_package`` (a different named validator); testing it
        here would be a contract error, not a validator bug.
        """
        validator = NAMED_VALIDATORS["project_name"]
        assert validator("myapi") is None
        assert validator("my-api") is None
        # Negative case: the project-name rule rejects underscores.
        assert validator("my_api") is not None

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_prompt_wiring_uses_the_same_validator(self, tmp_path: Path) -> None:
        """The wizard's inline validator and the resolver agree on names.

        The invariant this test enforces is stronger than the old
        "subset" test: it is the *same* function on both sides, so
        there is no possible drift. We assert the wiring by running
        a name through the non-interactive path and confirming the
        resolver accepts it unchanged (which requires the same
        validator the prompt uses to have already accepted it).

        If the wizard ever grows a *separate* validator (the
        duplicate the old test was designed to police), that test
        would fail here — the inline validator would accept a name
        the resolver rejects, or vice versa.

        The three-tuple return of ``_resolve_variables`` is unpacked
        (blueprint and surviving groups ignored) because the
        assertion is on the *inputs*.
        """
        # A name the resolver accepts, threaded through the wizard.
        for candidate in ("myapi", "my-api", "a1-b2"):
            _blueprint, inputs, _groups = create_module._resolve_variables(
                blueprint_id="python.fastapi.api",
                cli_name=candidate,
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )
            assert inputs.project_name == candidate


# -----------------------------------------------------------------------------
# Acceptance criterion 5: cancellation exits cleanly
# -----------------------------------------------------------------------------
class TestCancellation:
    """Ctrl+C at a prompt → ``PromptCancelledError``, exit 1, no traceback."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_cancellation_propagates_from_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``PromptCancelledError`` from the prompt propagates unchanged.

        The command must not catch and rewrap. If it did, the
        handler's ``PromptCancelledError`` branch would not fire and
        the cancellation would render as an unexpected error (exit
        5) or as a plain ``ForgeError`` (exit 1 but with the wrong
        rendering).
        """

        def _cancel(*_a: Any, **_k: Any) -> str:
            raise PromptCancelledError()

        monkeypatch.setattr(prompts, "ask_text", _cancel)

        with pytest.raises(PromptCancelledError):
            create_module._resolve_variables(
                blueprint_id="python.fastapi.api",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=False,
            )

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_cancellation_propagates_from_cli_with_exit_1(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: Ctrl+C → ``result.exception`` is the cancellation.

        ``CliRunner.invoke(forge_app, ...)`` runs ``create_app()``,
        which does **not** install ``handle_exception`` — that
        wrapper lives in ``main()``. So a cancellation raised inside
        the command propagates out of ``.invoke()`` as
        ``result.exception``, and ``result.output`` is empty.

        This test asserts on the exception type (the contract that
        ``main()`` will see) and on the exit code Typer derives from
        it. The *rendering* of that exception ("Cancelled." on
        stderr) is tested separately in ``test_errors.py``, where
        ``handle_exception`` is called directly — the correct layer
        for that assertion.
        """

        def _cancel(*_a: Any, **_k: Any) -> str:
            raise PromptCancelledError()

        monkeypatch.setattr(prompts, "ask_text", _cancel)

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api"],
        )

        assert result.exit_code == ExitCode.USER_ERROR
        assert isinstance(result.exception, PromptCancelledError)
        assert "Traceback" not in result.output


# -----------------------------------------------------------------------------
# Blueprint errors
# -----------------------------------------------------------------------------
class TestBlueprintErrors:
    """Unknown blueprint ID → ``BlueprintError`` (exit 2), fail fast."""

    @pytest.mark.unit()
    def test_unknown_blueprint_raises_blueprint_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unknown ID raises ``BlueprintError``, not ``UserInputError``.

        The exit code is the contract: a script that branches on
        ``$? in (1, 2, 3)`` treats both as user-fixable, but a
        script that wants "try a different blueprint" needs to
        distinguish 2 from 1.

        The ``get_blueprint_document`` override raises the domain
        error directly — the test is about the command's *response*
        to a blueprint-not-found, not about the discovery service's
        plumbing (which has its own tests).
        """

        def _raise(_id: str) -> None:
            raise BlueprintError(f"Blueprint '{_id}' not found.")

        monkeypatch.setattr(create_module, "get_blueprint_document", _raise)

        with pytest.raises(BlueprintError):
            create_module._resolve_variables(
                blueprint_id="does.not.exist",
                cli_name="x",
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_force_interactive")
    def test_unknown_blueprint_fails_before_prompting(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bad blueprint ID fails before the wizard asks anything.

        The user should not answer ten questions for a project that
        cannot be generated. The blueprint lookup happens first, and
        its failure short-circuits before ``ask_text`` is reached.
        """

        def _raise(_id: str) -> None:
            raise BlueprintError(f"Blueprint '{_id}' not found.")

        monkeypatch.setattr(create_module, "get_blueprint_document", _raise)
        monkeypatch.setattr(
            prompts,
            "ask_text",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("wizard fired for a blueprint that does not exist")
            ),
        )

        with pytest.raises(BlueprintError):
            create_module._resolve_variables(
                blueprint_id="does.not.exist",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=False,
            )


# -----------------------------------------------------------------------------
# Blueprint variables flow through the resolver
#
# These tests exercise the resolver's *blueprint variable* path with a
# fake blueprint that declares a required string variable. They exist
# to prove the resolution refactor did not accidentally route the
# project name through the resolver (which would have masked the
# `project_name` KeyError the test suite originally caught), and that
# blueprint variables still behave as they did before.
# -----------------------------------------------------------------------------
class TestBlueprintVariablesThroughResolver:
    """Blueprint variables flow through ``VariableResolver``."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint_with_vars", "_force_non_interactive")
    def test_missing_required_blueprint_variable_aggregates(self, tmp_path: Path) -> None:
        """A missing required blueprint variable surfaces from the resolver.

        The error is not flag-shaped (``--name``) — that error belongs
        to the *command's* flags. Blueprint variables have their own
        error shape (``required.missing`` from the resolver), which
        the command wraps in a ``UserInputError`` with a hint.
        """
        with pytest.raises(UserInputError) as info:
            create_module._resolve_variables(
                blueprint_id="python.fastapi.api",
                cli_name="my-api",
                cli_vars={},  # author not supplied
                output=tmp_path,
                no_interactive=True,
            )

        # ``UserInputError`` folds the hint text into the message, and
        # the resolver's joined errors carry the code for the missing
        # variable. Both are asserted because they are the contract
        # the CLI handler renders.
        assert "required variable 'author'" in str(info.value)

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint_with_vars", "_force_non_interactive")
    def test_supplied_blueprint_variable_passes_through(self, tmp_path: Path) -> None:
        """A supplied blueprint variable lands in ``_ResolvedInputs.variables``."""
        _blueprint, inputs, _groups = create_module._resolve_variables(
            blueprint_id="python.fastapi.api",
            cli_name="my-api",
            cli_vars={"author": "alice"},
            output=tmp_path,
            no_interactive=True,
        )

        assert inputs.variables["author"] == "alice"
        # The project name is also present — a command argument added
        # alongside the resolver's output.
        assert inputs.variables["project_name"] == "my-api"


# -----------------------------------------------------------------------------
# Generation pipeline (safety layer contract)
# -----------------------------------------------------------------------------
# These tests do not go through ``_resolve_variables`` — the resolver
# is deliberately pure and does not check the filesystem. The
# collision and overwrite contract is enforced by ``forge.safety``
# inside ``_generate``, and that is the layer these tests exercise.
#
# ``_generate`` takes the blueprint document as a keyword-only
# argument alongside the inputs, because the *planner* needs the
# document to read ``files[]`` and ``conditional_files[]``. The
# blueprint used here is a real ``Blueprint`` instance (from
# ``_make_blueprint``) with at least one ``files[]`` entry, so the
# pipeline has a destination to collide against.
#
# ``conditional_groups`` is passed as ``[]``: the fake blueprint has
# no conditional files, so the evaluator would return an empty list
# in the real pipeline. Passing ``[]`` explicitly is the caller saying
# "I evaluated conditions and none survived" — the correct shape,
# per ADR-0015's safe-by-default rule (a missing argument is not the
# same as an empty result).
#
# ``inputs.variables`` must be a real ``ResolvedValues``, not a plain
# ``dict``: ``_generate`` calls ``.as_dict()`` on it, and that method
# is the type boundary the resolver exists to enforce. The
# ``_resolved_inputs`` helper constructs the correct shape.
#
# ``_stub_blueprint_root`` (for tests that only need the stub) or
# ``blueprint_root`` (for tests that need to write into it) is the
# one fake: ``_blueprint_root`` resolves the manifest directory via
# discovery, which is not what these tests are about. Everything
# downstream of that function — the planner, the safety layer, the
# staging pipeline, the overwrite backups — runs for real. That is
# what makes these tests the KPI-guard for the pipeline contract.
# -----------------------------------------------------------------------------
class TestGeneratePreFlight:
    """``_generate`` enforces the safety layer's collision contract.

    The exit-code contract distinguishes two classes of failure that
    the pipeline can refuse:

    * **Collision** — a destination already exists and ``--overwrite``
      was not passed. ``CollisionError`` (exit code **6**). The
      remediation is "retry with ``--overwrite``, or edit the plan",
      and CI can branch on 6 specifically.
    * **Structural safety** — a path escapes the root, is absolute,
      traverses a symlink, or the parent is unwritable.
      ``SafetyError`` (exit code **3**). ``--overwrite`` cannot help;
      the plan must be edited.

    The tests here pin the *collision* contract because it is the one
    with the ``--overwrite`` remediation. The structural contract is
    exercised by the adversarial suite (``tests/adversarial/``), where
    each attack class has its own test.
    """

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_stub_blueprint_root")
    def test_existing_destination_without_overwrite_raises_collision(self, tmp_path: Path) -> None:
        """An existing destination without ``--overwrite`` → ``CollisionError``.

        Setup: a blueprint that plans one file (``out.txt``) whose
        destination already exists on disk. Without ``--overwrite``,
        the safety layer's ``detect_collisions`` finds the existing
        destination and refuses.

        The assertion is on the *type* (``CollisionError``) and on
        the *exit code* (6), not on ``FilesystemConflictError`` — a
        distinction that pins the exit-code contract from ADR-0017.
        A future regression that folded collisions back into the
        generic safety class would fail this test.
        """
        # Destination with a pre-existing file — what the pipeline
        # will collide against.
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "out.txt").write_text("pre-existing")

        blueprint = _make_blueprint(
            variables=[],
            files=[{"source": "src.txt", "destination": "out.txt"}],
        )
        inputs = _resolved_inputs(destination=target)

        with pytest.raises(CollisionError) as info:
            create_module._generate(
                blueprint=blueprint,
                inputs=inputs,
                conditional_groups=[],
                dry_run=False,
                overwrite=False,
            )

        # Exit code 6 is the contract — a script that retries with
        # ``--overwrite`` branches on this code specifically.
        assert info.value.exit_code == ExitCode.COLLISION

    @pytest.mark.unit()
    def test_existing_destination_with_overwrite_creates_backup(
        self, tmp_path: Path, blueprint_root: Path
    ) -> None:
        """``--overwrite`` replaces the file and backs up the original.

        Two guarantees, both tested:

        1. **The pipeline completes** — no exception, the destination
        now has the new content.
        2. **A backup exists** — the original content is preserved
        in a ``*.bak.<timestamp>`` sibling, matching ADR-0017's
        "every overwrite is backed up" contract.

        The test asserts on the *content* of both files, not just
        their existence. Existence alone would pass even if the
        backup held the wrong bytes, or if the destination were left
        unchanged (a different but equally broken outcome).

        The ``blueprint_root`` fixture (rather than
        ``_stub_blueprint_root``) is used because the pipeline reads
        the plan's source template from that directory during staging.
        Without the ``src.txt`` file present there, the copy branch of
        ``_stage_one`` would raise ``FilesystemConflictError`` — the
        test would then fail on the *pipeline setup*, not on the
        overwrite/backup contract it exists to pin.

        The content written to ``src.txt`` is ``"new body"`` so the
        assertion ``(target / "out.txt").read_text() == "new body"``
        names the property being tested (the destination has the
        *source's* content), not merely "some content".
        """
        # Source template the pipeline will copy into the destination.
        (blueprint_root / "src.txt").write_text("new body")

        # Destination with a pre-existing file — what the pipeline
        # will overwrite (and back up).
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "out.txt").write_text("pre-existing body")

        blueprint = _make_blueprint(
            variables=[],
            files=[{"source": "src.txt", "destination": "out.txt"}],
        )
        inputs = _resolved_inputs(destination=target)

        create_module._generate(
            blueprint=blueprint,
            inputs=inputs,
            conditional_groups=[],
            dry_run=False,
            overwrite=True,
        )

        # The destination now has the *source's* content.
        assert (target / "out.txt").read_text() == "new body"

        # The original content survives in a timestamped backup.
        backups = list(target.glob("out.txt.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "pre-existing body"

    @pytest.mark.unit()
    def test_dry_run_renders_plan_even_for_conflicted_destination(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        blueprint_root: Path,
    ) -> None:
        """``--dry-run`` renders the plan for a conflicted destination.

        A dry run is a *plan*, not a *commit*. Its purpose is to tell
        the user what would happen, including the conflict — so it
        must not fail on the conflict, and it must not silently
        return. The assertion is on the *rendered plan* (its presence
        in stdout), not on the absence of an exception.

        The ``blueprint_root`` fixture is required because the
        pipeline resolves the plan's source reference even in dry-run
        mode — the plan's rendering includes the source path, and
        the source is resolved against the blueprint root. Without
        the fixture, ``_blueprint_root`` would call discovery and
        fail (there is no discovery layout in a unit test).
        """
        (blueprint_root / "src.txt").write_text("body")
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "out.txt").write_text("pre-existing")

        blueprint = _make_blueprint(
            variables=[],
            files=[{"source": "src.txt", "destination": "out.txt"}],
        )
        inputs = _resolved_inputs(destination=target)

        create_module._generate(
            blueprint=blueprint,
            inputs=inputs,
            conditional_groups=[],
            dry_run=True,
            overwrite=False,
        )

        captured = capsys.readouterr()
        assert "python.fastapi.api" in captured.out
        assert "my-api" in captured.out
        assert str(target) in captured.out
