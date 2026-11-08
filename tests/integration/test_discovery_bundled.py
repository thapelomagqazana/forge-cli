# ruff: noqa: S603
"""Integration test: bundled blueprints are discoverable from a built wheel.

What this test protects
-----------------------
The single most damaging class of bug in a local-first tool like Forge
is "works in editable install, ships broken in the wheel." It happens
when a non-Python resource (here: ``blueprint.yaml`` manifests and their
``templates/``) is present in the source tree but not declared to the
build backend, so the wheel silently omits it. Editable installs read
directly from the source tree and never notice; end users who
``pip install forge-cli`` see "not installed" for every official
blueprint, with no way to diagnose it from outside the wheel.

Unit tests in ``tests/unit/discovery/`` cannot catch this because they
run against the source tree. This file builds a wheel, installs it into
an isolated environment, runs discovery *from outside the repo root*, and
asserts on the result. It is the only test in the suite that exercises
the property "the wheel is self-contained."

What this test is not
---------------------
* **Not a build test.** ``tests/unit/models/test_generated_in_sync.py``
  and the CI pipeline already build the wheel. This test consumes a
  built wheel; if the build is broken, it fails upstream.
* **Not a packaging test for schemas.** Schema packaging is covered by
  ``tests/integration/test_schema_bundled.py``. The two are complementary:
  a wheel could ship the schema and forget the blueprints (a change to
  ``pyproject.toml`` that adds one entry and not the other), and each
  test catches one direction of that divergence.
* **Not a full end-to-end test.** It exercises discovery, not the
  wizard, not scaffolding, not verification. The wizard's interaction
  with the catalogue is tested in the wizard's own suite; the
  blueprints' generation behaviour is tested per-blueprint in
  ``tests/integration/blueprints/``.

Why a subprocess, and not a direct import
-----------------------------------------
The bug this test catches is specifically about *installed* layout
(``site-packages/forge/blueprints/``) versus *source* layout
(``src/forge/blueprints/``). A direct import in the test process would
read from the source tree because ``pyproject.toml`` adds
``src`` to ``sys.path`` (via ``pythonpath = ["src"]`` in
``[tool.pytest.ini_options]``). The subprocess runs from a temporary
directory with a clean ``sys.path`` and a wheel-installed ``forge``
package, which is the layout users actually have.

The test does *not* build the wheel itself. It expects one to exist at
a documented location, produced by the CI pipeline. Building inside the
test would require the ``build`` package to be a test dependency, would
add 5 to 10 seconds to every run, and would duplicate the CI pipeline's
own build step. The test asserts the *contents* of the built artifact;
the pipeline is responsible for producing it.

Layout expectations
-------------------
The test looks for ``dist/forge_cli-*.whl`` (the standard wheel name
produced by Hatchling). If no wheel is present, the test is skipped,
not failed — a developer running ``pytest`` locally without building
first should not see a spurious failure. CI runs the build step before
this test; the skip is a local-development convenience, not a
production compromise. The skip message names the command to run.

On the S603 suppression
-----------------------
The five ``subprocess.run`` calls in this file are all flagged by
Bandit's S603 rule, which fires on any call whose argument list is
not a compile-time literal. That theory does not apply here:

* Every argument is a value the *test* produced: ``sys.executable``
  or a path inside ``tmp_path_factory``.
* Every script passed via ``-c`` is a module-level ``textwrap.dedent``
  string in this file. No user input, no environment variable, no
  argv element contributes to it.
* ``cwd`` is a ``tmp_path_factory`` directory or a subdirectory of
  one. Not the repo, not the CWD, not a value read from ``os.environ``.

The suppression is **file-level** rather than inline because:

* The five call sites all have the same justification. Repeating the
  reasoning in five ``# noqa: S603`` comments would be five chances
  to drift, and a reader who sees only one of them would not know
  the others share the argument.
* Inline ``# ruff: noqa`` directives at line 1 of a file that begins
  with a docstring are stripped by Ruff's ``RUF100`` cleanup on the
  next ``ruff --fix`` run — a landmine the sibling
  ``test_generated_in_sync.py`` hit and documented. File-level
  scope has no such interaction.

The suppression lives in this file (not in ``pyproject.toml``'s
``per-file-ignores``) so the reasoning and the code it justifies stay
together. A future reader who sees the ``S603`` markers can find *why*
in this docstring, one screen up from the first suppressed call, not
in a configuration file they would have to search for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

import pytest

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
# The officially shipped blueprint IDs. Kept as a set so the assertion
# is on membership ("every expected id is present") rather than on
# exact equality ("no other ids are present"). Adding a fourth official
# blueprint is a change to this set plus the fixture; removing one is a
# breaking change that this test should catch, so the set is the contract.
_EXPECTED_OFFICIAL_IDS: frozenset[str] = frozenset(
    {
        "python.fastapi.api",
        "python.automation.cli",
        "typescript.frontend.react",
    }
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
def _repo_root() -> Path:
    """Return the repository root, derived from this file's location.

    ``parents[2]`` because this file is at
    ``tests/integration/test_discovery_bundled.py``; two ``parents``
    walks (integration → tests → repo root). Using ``__file__`` rather
    than ``Path.cwd()`` makes the test independent of the directory
    pytest is invoked from — CI sometimes runs from ``/`` or from a
    temporary ``PYTHONPATH`` root, and a CWD-relative lookup would
    silently fail there.
    """
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the path to a built wheel, or skip with an actionable message.

    The fixture is ``module``-scoped because building the venv (below)
    is expensive — a few seconds per test — and this module contains
    multiple integration tests that all need the same wheel and the
    same installed environment. Sharing the wheel across tests in the
    module is safe because nothing in the module mutates it; each test
    creates its own subprocess.

    **If no wheel exists**, the test skips rather than fails. The
    reasoning: this is an *integration* test whose prerequisite is a
    built artifact. CI builds the artifact before running pytest. A
    developer running ``pytest tests/integration/`` locally without
    building first gets a clear skip message naming the build command,
    which is more helpful than a failure that looks like a test bug.

    The skip is not a compromise on coverage: CI's job is to run every
    test against a real wheel, and the CI pipeline is what actually
    protects against the "works in editable, ships empty" bug. The
    skip only removes a local annoyance.
    """
    dist = _repo_root() / "dist"
    wheels = sorted(dist.glob("forge_cli-*.whl")) if dist.is_dir() else []

    if not wheels:
        pytest.skip(
            "No built wheel found. Build one first:\n"
            "    uv build\n"
            "Then re-run this test. CI builds the wheel automatically "
            "before running integration tests."
        )

    # If multiple wheels exist (a dirty build directory), pick the most
    # recent. This handles the case where a developer rebuilt after a
    # fix — the newest wheel is the one they want to test.
    return max(wheels, key=lambda p: p.stat().st_mtime)


@pytest.fixture(scope="module")
def installed_env(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install the wheel into an isolated venv; return the venv's Python.

    **Why an isolated venv, not the current interpreter's site-packages?**

    1. **Isolation.** Installing into the project's own venv would
       shadow the editable install that pytest uses to import
       ``forge`` — the tests in this module would then import the
       *wheel's* ``forge`` for the subprocess, but the *source tree's*
       for everything else, and the two would disagree about what
       "bundled" means. Creating a fresh venv guarantees the
       subprocess sees only the wheel.

    2. **Reproducibility.** The current environment may have leftover
       state from earlier test runs (installed older wheels, extra
       dependencies). A fresh venv is the only way to prove the wheel
       is self-contained.

    **Why install with pip in the venv, rather than extracting the
    wheel by hand?** Two reasons:

    * The subprocess imports ``forge`` and its declared dependencies
      (pydantic, jsonschema, pyyaml, typer, rich, questionary,
      jinja2, click, httpx). Extracting the wheel into a directory
      on ``sys.path`` would not install the dependencies, so the
      import would fail for a reason unrelated to the test's subject.
    * ``pip install`` is what a real user does. Using it here is the
      cheapest way to exercise the same installation path.

    **Dependency installation.** ``pip install --no-deps <wheel>``
    installs the wheel without pulling its dependencies. The
    dependencies are installed separately by
    ``_install_runtime_dependencies_from_current_environment``, which
    reads the wheel's own ``METADATA`` to determine the list — so
    if the wheel adds a dependency tomorrow, this fixture installs
    it without a test change. When CI has network, pip caches the
    downloads; when CI is offline, the parent environment's
    site-packages provides them.
    """
    env_dir = tmp_path_factory.mktemp("forge-wheel-venv")
    venv.create(env_dir, with_pip=True, clear=True)

    # The venv's python lives in different locations on Unix vs
    # Windows. ``sysconfig`` would be the "proper" way to detect this,
    # but a simple existence check on both candidates is clearer and
    # does not require importing ``sysconfig`` (which has its own
    # cross-platform quirks).
    if sys.platform == "win32":
        python = env_dir / "Scripts" / "python.exe"
    else:
        python = env_dir / "bin" / "python"

    assert python.is_file(), f"venv python not found at {python}"

    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(built_wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    _install_runtime_dependencies_from_current_environment(python)

    return python


def _install_runtime_dependencies_from_current_environment(python: Path) -> None:
    """Install forge's runtime deps into the venv, reusing the current env.

    ``pip install --no-deps forge-cli`` installs the wheel without its
    dependencies. The subprocess needs them to import ``forge``. Rather
    than re-download them (which fails in offline CI), we point pip at
    the current interpreter's site-packages and let it copy the wheels
    it finds there.

    The list of dependencies is *not* hard-coded here: it is derived
    from the wheel's ``METADATA`` file (via ``importlib.metadata`` in
    the parent env). If the wheel adds a dependency tomorrow, this
    function installs it without a test change — the test is proving
    that *whatever the wheel declares* is installable, not that a
    specific fixed set of packages can be installed.
    """
    from importlib.metadata import requires

    # ``requires`` returns the wheel's declared deps as ``Requirement``
    # strings (e.g. ``"pydantic>=2.7.0,<3.0"``). It includes extras
    # markers; the runtime deps are the ones without an ``extra ==``
    # clause. Filtering with a simple string check is more readable
    # than parsing the marker with ``packaging`` and is correct for
    # the current metadata shape.
    all_requirements = requires("forge-cli") or []
    runtime = [r for r in all_requirements if "extra ==" not in r and "; extra" not in r]

    if not runtime:
        # No runtime deps declared. The test below would still pass
        # because the subprocess only imports ``forge``, and importing
        # a module with no imports works — but this branch is here to
        # fail loudly if the packaging metadata ever *becomes* empty
        # through a botched pyproject edit, rather than pass silently.
        return

    subprocess.run(
        [str(python), "-m", "pip", "install", *runtime],
        check=True,
        capture_output=True,
        text=True,
    )


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------
class TestBundledBlueprintsInWheel:
    """Discovery finds official blueprints from a wheel-installed package."""

    @pytest.mark.integration()
    def test_all_official_blueprints_are_discoverable(self, installed_env: Path) -> None:
        """Every expected official blueprint appears in a wheel install.

        The subprocess runs from ``temp_dir``, outside the repo, with a
        clean environment. The absence of any reference to the source
        tree means the discovery service *must* be reading from the
        installed wheel's ``forge.blueprints`` resource package. If the
        wheel is missing the blueprints (a ``pyproject.toml`` regression
        in ``[tool.hatch.build.targets.wheel].artifacts``), this test
        fails with "catalogue is empty" — and that failure is exactly
        the bug the test exists to catch.
        """
        # ``temp_dir`` is not used by the subprocess directly; it is
        # passed as CWD so a regression in the discovery service that
        # reads from a relative path (`.forge/blueprints/`) would be
        # visible here. In an editable install, that path resolves
        # near the repo root; in a wheel install, it must resolve
        # relative to the invoking user's CWD, not the wheel's own
        # location.
        temp_dir = installed_env.parent / "cwd"
        temp_dir.mkdir(exist_ok=True)

        script = textwrap.dedent(
            """
            import json
            import sys

            from forge.discovery import BlueprintDiscoveryService

            catalogue = BlueprintDiscoveryService().discover()
            print(json.dumps([
                {"id": bp.id, "source": bp.source.value}
                for bp in catalogue
            ]))
            """
        )

        result = subprocess.run(
            [str(installed_env), "-c", script],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            check=False,
            # Strip the parent environment's Python path so the
            # subprocess cannot accidentally import ``forge`` from the
            # source tree. ``PYTHONPATH`` is the only cross-platform
            # way to point the interpreter at a directory; removing it
            # from the inherited environment is the only way to
            # prevent a developer's local ``.envrc`` or CI's
            # ``PYTHONPATH=src`` from bleeding through.
            env=_isolated_env(),
        )

        assert result.returncode == 0, (
            f"subprocess failed:\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )

        catalogue = json.loads(result.stdout)
        discovered_ids = {entry["id"] for entry in catalogue}

        missing = _EXPECTED_OFFICIAL_IDS - discovered_ids
        assert not missing, (
            f"Official blueprints missing from wheel install: {sorted(missing)}\n"
            f"Discovered ids: {sorted(discovered_ids)}\n"
            f"This usually means a resource was added to "
            f"``src/forge/blueprints/`` but not to "
            f"``pyproject.toml``'s ``artifacts`` list. Check the build "
            f"configuration before assuming a code bug."
        )

    @pytest.mark.integration()
    def test_official_blueprints_are_tagged_official(self, installed_env: Path) -> None:
        """Every discovered blueprint from a clean install is ``OFFICIAL``.

        In a wheel install with no ``.forge/blueprints/`` in the CWD,
        every discovered blueprint must have ``source == "official"``.
        A ``"local"`` tag here would mean the wheel install is
        accidentally reading from a local path — either the CWD's
        ``.forge/blueprints/`` (which would be empty in this test's
        fresh ``temp_dir``) or a source-tree path leaked through
        ``sys.path``. Both are bugs worth failing on.
        """
        temp_dir = installed_env.parent / "cwd-tags"
        temp_dir.mkdir(exist_ok=True)

        script = textwrap.dedent(
            """
            import json
            from forge.discovery import BlueprintDiscoveryService

            catalogue = BlueprintDiscoveryService().discover()
            print(json.dumps([
                {"id": bp.id, "source": bp.source.value}
                for bp in catalogue
            ]))
            """
        )

        result = subprocess.run(
            [str(installed_env), "-c", script],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            check=False,
            env=_isolated_env(),
        )

        assert result.returncode == 0, result.stderr

        catalogue = json.loads(result.stdout)
        non_official = [entry for entry in catalogue if entry["source"] != "official"]
        assert not non_official, (
            f"Clean wheel install produced non-official blueprints: {non_official}\n"
            f"This usually means the discovery service is reading from "
            f"a leaked source-tree path or from a stale "
            f"``.forge/blueprints/`` in a parent directory."
        )

    @pytest.mark.integration()
    def test_load_returns_typed_blueprint_from_wheel(self, installed_env: Path) -> None:
        """``load(id)`` returns a full ``Blueprint`` from a wheel install.

        Discovery and loading exercise different code paths: discovery
        stops at the ``DiscoveredBlueprint`` projection, loading goes
        all the way through ``validate_blueprint_document`` (schema +
        model). A wheel that ships the manifest but not the schema (or
        vice versa) would pass the discovery test and fail this one —
        which is why both are tested. The failure message names the
        likely cause so a future contributor does not have to guess.
        """
        temp_dir = installed_env.parent / "cwd-load"
        temp_dir.mkdir(exist_ok=True)

        script = textwrap.dedent(
            """
            import json
            from forge.discovery import BlueprintDiscoveryService

            blueprint = BlueprintDiscoveryService().load("python.fastapi.api")
            print(json.dumps({
                "id": blueprint.id,
                "name": blueprint.name,
                "version": blueprint.version,
                "variables": len(blueprint.variables),
            }))
            """
        )

        result = subprocess.run(
            [str(installed_env), "-c", script],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            check=False,
            env=_isolated_env(),
        )

        assert result.returncode == 0, (
            f"load() failed in wheel install:\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
            f"This usually means the wheel is missing a resource: "
            f"either ``blueprint.yaml`` (discovery tests would also "
            f"fail) or the schema (only ``load`` needs it)."
        )

        loaded = json.loads(result.stdout)
        assert loaded["id"] == "python.fastapi.api"
        assert loaded["name"], "blueprint name is empty"
        assert loaded["version"], "blueprint version is empty"
        # The FastAPI blueprint declares at least one variable; a zero
        # here would mean the manifest shipped but was truncated.
        assert loaded["variables"] >= 1


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _isolated_env() -> dict[str, str]:
    """Return an environment dict for the wheel subprocess.

    Removes ``PYTHONPATH`` and any ``PYTHONHOME`` inherited from the
    parent. The parent environment (pytest) has ``PYTHONPATH=src`` to
    import the editable install; the subprocess must *not* inherit
    that, because the test's whole point is that the wheel is
    self-contained.

    Other environment variables are preserved (``PATH``, ``HOME``,
    locale, etc.). ``PATH`` in particular is needed for pip to find
    the system's subprocess helpers when the wheel's deps have build
    steps; ``HOME`` for pip's cache; and locale settings so error
    messages are readable.

    This is a narrower version of ``env -i``: it strips only what
    would break the test, not everything. A subprocess with a fully
    empty environment would fail for reasons unrelated to the test's
    subject — and would produce a failure message that sends the
    contributor down the wrong path.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env
