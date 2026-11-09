"""Guard: ``BlueprintStatus`` has exactly one definition.

Why this test exists
--------------------
``BlueprintStatus`` is exported from three places:

1. ``forge.models.status``               — the definition site (home).
2. ``forge.cli.schemas.list_output``     — re-exported for the DTO.
3. ``forge.discovery``                   — re-exported for the service
                                           return type.

If a future refactor adds a *second* ``class BlueprintStatus`` anywhere
in the tree, the duplicate would:

* have identical members, so ``==" comparisons by value still pass,
* pass ``isinstance(x, ...)`` only against whichever class the caller
  happened to import,
* serialise to the same JSON, so JSON-level tests would not notice,
* break ``dict`` / ``set`` keying subtly (two "equal" classes are not
  hashable-compatible across import paths),
* and reintroduce the exact CLI↔discovery coupling that ADR-0009
  forbids, because at least one of the definitions would have to live
  in ``forge.cli`` to be importable from there.

None of those failures would be caught by ordinary behaviour tests.
This module is the guard that makes a duplicate a *test failure with a
stack trace pointing at the offending module*, not a subtle bug
discovered weeks later in a downstream consumer.

What this file covers
---------------------
* **Identity across import paths.** All three import sites must yield
  the *same class object*.
* **Home module.** The definition must live in ``forge.models.status``,
  not in ``forge.cli.schemas.list_output`` or ``forge.models.blueprint``.
* **No duplicate definitions on disk.** A static scan of ``src/forge``
  finds exactly one ``class BlueprintStatus`` statement.
* **No forbidden imports.** ``forge.discovery`` and ``forge.models``
  must not import from ``forge.cli``.
* **Member set is a wire contract.** Names and string values are what
  ``forge list --json`` emits; renames and removals are consumer-
  visible breaking changes and require a schema-version bump.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The three supported import paths. Each one is a supported public API
# and each is tested separately, so a regression in *any one* of them
# fails with a clear message rather than as a side effect of a test
# for a different path.
from forge.cli.schemas.list_output import BlueprintStatus as StatusFromSchemas
from forge.discovery import BlueprintStatus as StatusFromDiscovery
from forge.models.status import BlueprintStatus as StatusFromModels

# Repo root: this file lives at ``tests/unit/models/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src" / "forge"


# ---------------------------------------------------------------------------
# Identity — one class, three import paths
# ---------------------------------------------------------------------------


class TestIdentity:
    """All three import paths must resolve to the *same class object*.

    ``is`` (not ``==``) is intentional: ``==`` on two ``Enum`` classes
    is a ``TypeError`` (classes are not values), and on the *members*
    it would pass even if the classes were distinct — which is exactly
    the bug this test exists to catch.
    """

    def test_schemas_reexport_is_identity(self) -> None:
        assert StatusFromSchemas is StatusFromModels, (
            "forge.cli.schemas.list_output must re-export the class "
            "from forge.models.status; if this fails, a second "
            "definition has been introduced."
        )

    def test_discovery_reexport_is_identity(self) -> None:
        assert StatusFromDiscovery is StatusFromModels, (
            "forge.discovery must re-export the class from "
            "forge.models.status; if this fails, a second definition "
            "has been introduced."
        )

    def test_all_three_paths_are_the_same_object(self) -> None:
        # Kept as a separate test because it is the one a reviewer will
        # grep for when investigating a duplicate. Splitting the pairs
        # above makes the failure message precise; this combined test
        # makes the invariant obvious at a glance.
        assert StatusFromSchemas is StatusFromDiscovery is StatusFromModels


# ---------------------------------------------------------------------------
# Home module
# ---------------------------------------------------------------------------


class TestHomeModule:
    """The definition must live in ``forge.models.status``.

    The home module is not an implementation detail — it is the whole
    reason the class exists in its own file (see the module docstring
    of ``forge.models.status``). Moving it back into a consumer is a
    layering violation, and this test names the rule so the violation
    is caught in review, not in a downstream import cycle.
    """

    def test_definition_lives_in_models_status(self) -> None:
        assert StatusFromModels.__module__ == "forge.models.status", (
            "BlueprintStatus is defined in "
            f"{StatusFromModels.__module__!r}; it must live in "
            "'forge.models.status' so that forge.discovery can import it "
            "without depending on forge.cli (ADR-0009)."
        )

    def test_definition_is_not_in_the_generated_blueprint_module(self) -> None:
        # ``forge.models.blueprint`` is generated by datamodel-codegen
        # (ADR-0008). Hand-adding a class there is erased on the next
        # ``make models`` run — and, worse, would make the class appear
        # to live in a file that the generation pipeline owns. If this
        # assertion ever fails, someone has bypassed the generation
        # contract.
        assert StatusFromModels.__module__ != "forge.models.blueprint", (
            "BlueprintStatus must not be defined in the generated "
            "blueprint model; put it in forge.models.status and "
            "re-export from consumers."
        )

    def test_definition_is_not_in_the_cli_schemas_module(self) -> None:
        # The original location. Documented here because the fix that
        # moved the class out was driven by a layering violation, not
        # by a stylistic preference — a future contributor who moves it
        # back needs to reckon with ADR-0009 explicitly.
        assert StatusFromModels.__module__ != "forge.cli.schemas.list_output", (
            "BlueprintStatus was moved out of forge.cli.schemas "
            "because forge.discovery needs it and may not import from "
            "forge.cli (ADR-0009). Do not move it back."
        )


# ---------------------------------------------------------------------------
# No duplicate class definitions anywhere on disk
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> list[Path]:
    """Yield every ``.py`` file under ``root``, sorted for determinism."""
    return sorted(root.rglob("*.py"))


# The regex matches ``class BlueprintStatus`` at the start of a logical
# line, with any leading whitespace. ``re.MULTILINE`` is unnecessary
# because we search each file's full text; ``^`` is anchored manually
# by splitting lines so we can also report the line number.
_CLASS_PATTERN = re.compile(r"^\s*class\s+BlueprintStatus\b", re.MULTILINE)


class TestNoDuplicateDefinition:
    """Exactly one ``class BlueprintStatus`` in ``src/forge``."""

    def test_exactly_one_class_definition_in_source_tree(self) -> None:
        hits: list[tuple[Path, int]] = []
        for path in _iter_python_files(_SRC_ROOT):
            text = path.read_text(encoding="utf-8")
            for match in _CLASS_PATTERN.finditer(text):
                # ``text.count('\n', 0, start)`` gives the line number
                # of the match start; +1 to make it 1-indexed.
                line = text.count("\n", 0, match.start()) + 1
                hits.append((path.relative_to(_REPO_ROOT), line))

        assert len(hits) == 1, (
            "Expected exactly one 'class BlueprintStatus' definition in "
            f"src/forge, found {len(hits)}:\n"
            + "\n".join(f"  {path}:{line}" for path, line in hits)
        )
        only_path, _line = hits[0]
        assert only_path == Path("src/forge/models/status.py"), (
            f"The single definition must live in "
            f"src/forge/models/status.py, but was found in {only_path}."
        )


# ---------------------------------------------------------------------------
# Forbidden import directions
# ---------------------------------------------------------------------------


# The pattern matches any module-level or function-level import from
# ``forge.cli`` (including ``from forge.cli ...`` and
# ``import forge.cli ...``). It deliberately does *not* match docstring
# mentions such as ``forge.cli.schemas``, because those are prose — the
# test's job is to catch real imports.
_FORBIDDEN_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from|import)\s+forge\.cli\b",
    re.MULTILINE,
)


class TestForbiddenImportDirections:
    """``forge.discovery`` and ``forge.models`` must not import the CLI.

    ADR-0009 states: *"Nothing in this package imports the CLI."* That
    direction exists so ``forge.discovery`` can be extracted into its
    own distribution later without dragging the CLI in with it, and so
    a future non-CLI consumer (an HTTP catalog endpoint, an editor
    integration) does not inherit CLI schemas.

    ``forge.models`` inherits the same rule for the same reason: it is
    the shared vocabulary layer that both ``discovery`` and ``cli``
    depend on, and a ``models → cli`` edge would invert the layering.

    Enforcement here (rather than via a linter plugin) keeps the rule
    *in this project* with a message that references the ADR, rather
    than dependent on a third-party linter rule whose name may change.
    """

    @pytest.mark.parametrize(
        "package_subdir",
        ["discovery", "models"],
    )
    def test_no_forge_cli_imports(self, package_subdir: str) -> None:
        package_root = _SRC_ROOT / package_subdir
        offenders: list[tuple[Path, int, str]] = []

        for path in _iter_python_files(package_root):
            text = path.read_text(encoding="utf-8")
            for match in _FORBIDDEN_IMPORT_PATTERN.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                # Capture just the import line for the error message.
                line_text = text.splitlines()[line - 1].strip()
                offenders.append((path.relative_to(_REPO_ROOT), line, line_text))

        assert not offenders, (
            f"forge.{package_subdir} must not import forge.cli "
            "(ADR-0009). Offending imports:\n"
            + "\n".join(f"  {p}:{n}: {s}" for p, n, s in offenders)
        )


# ---------------------------------------------------------------------------
# Member set — the wire contract
# ---------------------------------------------------------------------------


class TestMemberSet:
    """The enum's members are a public contract.

    ``forge list --json`` serialises each member's ``.value`` as a
    string (the renderer calls ``model_dump(mode="json")``, and the
    member is a ``str`` subclass, so it serialises to its string value
    — not to ``{"value": ...}``). That means the *set of string values*
    is what downstream automation depends on.

    Renaming a member or changing its value is a **breaking change** for
    consumers and requires a schema-version bump in ``ListOutput`` (see
    ``CLI_LIST_OUTPUT.md``). Adding a new member is additive and safe —
    consumers that ignore unknown values keep working.
    """

    def test_values_are_stable_strings(self) -> None:
        assert {m.value for m in StatusFromModels} == {"ok", "stale", "error"}, (
            "BlueprintStatus values are part of the forge list --json "
            "contract. Adding a value is fine; renaming or removing one "
            "is a breaking change and requires bumping ListOutput's "
            "schema_version."
        )

    def test_members_subclass_str(self) -> None:
        # JSON serialisation depends on the members being ``str``
        # subclasses. ``json.dumps(StatusFromModels.ok)`` returns
        # ``'"ok"'`` only because the class inherits from ``str``; if a
        # future refactor drops the ``str`` base, every ``--json``
        # consumer breaks. This is a one-line invariant that has been
        # accidentally broken in sibling projects.
        assert issubclass(StatusFromModels, str), (
            "BlueprintStatus must subclass str so that its members "
            "serialise to their string values in JSON output."
        )

    def test_members_are_hashable(self) -> None:
        # Dict / set keying by status is used by the loader and by
        # tests. ``Enum`` members are hashable by default, but the
        # assertion documents the expectation and would catch a future
        # refactor that replaces the enum with a dataclass or a plain
        # class without ``__hash__``.
        assert all(hash(m) is not None for m in StatusFromModels)
