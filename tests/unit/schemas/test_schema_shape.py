"""Tests for the shape of the bundled Blueprint JSON Schema itself.

These tests answer a different question than ``test_schema_accepts`` /
``test_schema_rejects``:

    * Those tests ask "does the schema correctly judge a *document*?"
    * These tests ask "is the schema itself a well-formed JSON Schema?"

The distinction matters because a schema can be syntactically valid
JSON, loadable by ``json.loads``, and still be **invalid as a schema**
— e.g. a ``$ref`` pointing at a non-existent definition, a ``pattern``
containing a regex group name Python's ``re`` rejects, or a
``required`` array listing a property that does not exist. Those bugs
would otherwise surface as confusing runtime failures on the *first
blueprint* someone validates, not at build time.

The schema is the single source of truth for what a blueprint is
(ADR-0007). Tests that guard its shape are therefore the cheapest
insurance in the codebase: they run in milliseconds and they fail
before a broken schema ever reaches a wheel.

Design notes
------------
* **No fixtures beyond the schema itself.** These tests read the
  bundled schema through the public loader — they do not open the file
  directly. That is deliberate: it exercises the same code path the
  verification layer uses, so a packaging regression (schema missing
  from the wheel) fails here *and* in the loader, not just in one
  place.
* **Assertions target semantic properties, not exact strings.** A
  test that asserts ``schema["required"] == ["id", ...]`` would need
  editing every time a required field is added. A test that asserts
  "``id`` is in ``required``" continues to pass. Tests that encode
  intent survive refactors; tests that encode today's data do not.
* **Every test names the *consequence* of failure.** The docstring of
  each test answers "what breaks if this fails?" — so a red CI run
  tells a future contributor whether they need to fix a real bug or
  update a stale expectation.
"""

from __future__ import annotations

import re

from jsonschema import Draft202012Validator

from forge.schemas import load_schema, schema_version


# =============================================================================
# Meta-schema conformance
# =============================================================================
def test_schema_is_valid_draft_2020_12() -> None:
    """The schema must itself validate against the Draft 2020-12 meta-schema.

    ``Draft202012Validator.check_schema`` is the authoritative test:
    it validates the schema *against the meta-schema* the ``$schema``
    keyword declares. A schema that fails this check will be rejected
    the first time a validator is constructed in production — with a
    traceback from deep inside ``jsonschema`` that is hard to map back
    to the offending line.

    Consequences if this fails:
        * ``forge.verification.handlers._build_validator`` raises on
          first use.
        * Every ``forge create`` invocation fails before any user
          interaction.
        * The failure mode is ``INTERNAL_ERROR`` (exit 5), which is
          correct (it *is* a Forge bug) but expensive to diagnose.
    """
    schema = load_schema()
    Draft202012Validator.check_schema(schema)


def test_schema_declares_its_own_version() -> None:
    """The ``schema_version`` property must be a ``const`` matching the loader.

    Two things must be true and must stay in lockstep:

    * The schema declares ``schema_version`` with ``"const": "<v>"``
      (a *fixed* value, not a pattern — the loader reads this exact
      value).
    * ``forge.schemas.schema_version()`` returns the same string.

    If they diverge, a document authored against version N will pass
    the loader's version check but fail schema validation — or vice
    versa. The two representations must be derived from the same
    source, which is why the schema itself is the source.
    """
    schema = load_schema()
    declared = schema["properties"]["schema_version"]["const"]
    assert declared == schema_version(), (
        "schema_version() disagrees with the schema's own const: "
        f"{schema_version()!r} vs {declared!r}"
    )


def test_schema_version_is_semver() -> None:
    """``schema_version`` must be a full SemVer string, not a prefix.

    A schema version like ``"1.0"`` would work for the loader's
    equality check but would block future additive evolution: you
    cannot distinguish "1.0.1 fixes a bug" from "1.1.0 adds a field"
    from "2.0.0 breaks compatibility" without the full triple.
    """
    semver = re.compile(
        r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)" r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
    )
    assert semver.match(schema_version()), f"schema_version {schema_version()!r} is not SemVer"


def test_schema_id_is_stable_and_versioned() -> None:
    """``$id`` must be an absolute URI and must embed the schema version.

    The ``$id`` is what ``jsonschema`` uses to cache the compiled
    schema and to resolve relative ``$ref`` targets. It must be:

    * **Absolute** — a relative ``$id`` is undefined behaviour in
      Draft 2020-12 (the base URI is host-dependent).
    * **Versioned** — the version must appear in the URI so that a
      future v2 schema can be loaded side-by-side without either
      shadowing the other in ``jsonschema``'s internal cache.

    Consequences if this fails:
        * Two schema versions could collide in ``jsonschema``'s
          resolver cache, producing cross-contamination bugs that are
          invisible until the second version is loaded.
    """
    schema = load_schema()
    schema_id = schema["$id"]
    assert schema_id.startswith("https://"), f"$id must be an absolute https URI, got {schema_id!r}"
    assert schema_version() in schema_id, (
        f"$id {schema_id!r} does not embed schema_version "
        f"{schema_version()!r}; a future v2 schema could collide"
    )


# =============================================================================
# Structural invariants
# =============================================================================
def test_schema_forbids_unknown_top_level_fields() -> None:
    """``additionalProperties: false`` must be set at the top level.

    This is what makes the "unknown field" rejection in
    ``test_schema_rejects.py`` pass. If someone removes this to make
    a new field "just work", unknown fields stop being rejected and
    the schema silently loses a security property: a typo in a field
    name (``varibales:``) would be accepted as an extension rather
    than rejected as a bug.
    """
    schema = load_schema()
    assert schema.get("additionalProperties") is False, (
        "top-level additionalProperties must be False so that "
        "unknown fields are rejected, not silently ignored"
    )


def test_schema_declares_required_fields() -> None:
    """Every field the loader depends on must be in ``required``.

    The loader reads ``properties.schema_version.const``, and the
    verification layer reads the full top-level structure. A field
    that is *declared* but not *required* is a footgun: a document
    can omit it, the loader will still try to read it, and the crash
    happens at runtime rather than at validation time.
    """
    schema = load_schema()
    required = set(schema.get("required", []))
    for field in (
        "schema_version",
        "id",
        "name",
        "version",
        "description",
        "category",
        "variables",
        "files",
        "verification",
        "compatibility",
    ):
        assert field in required, (
            f"{field!r} must be in top-level required; omitting it "
            f"lets a document pass validation while crashing the loader"
        )


def test_schema_uses_defs_not_definitions() -> None:
    """Draft 2020-12 uses ``$defs``, not ``definitions``.

    ``definitions`` was the Draft-07 keyword and is still *tolerated*
    by ``jsonschema`` as a backwards-compatibility alias. But
    tolerating it means a future reader of this schema could not tell
    which draft it targets without checking ``$schema``. Using
    ``$defs`` makes the draft explicit at a glance — and if the alias
    is ever removed from ``jsonschema``, this test ensures the schema
    is already correct.
    """
    schema = load_schema()
    assert "$defs" in schema, "Draft 2020-12 schema must use '$defs'"
    assert "definitions" not in schema, (
        "Draft 2020-12 replaces 'definitions' with '$defs'; "
        "the alias is deprecated and will be removed"
    )


# =============================================================================
# Cross-reference integrity
# =============================================================================
def test_all_refs_point_to_existing_defs() -> None:
    """Every internal ``$ref`` must resolve to a real ``$defs`` entry.

    A dangling ``$ref`` is the most common schema bug: it is
    syntactically valid JSON, loadable by ``json.loads``, and passes
    ``check_schema`` on most versions of ``jsonschema`` — but it
    fails *at validation time* with a ``RefResolutionError`` the
    first time a document reaches the unresolved branch.

    Walking the tree manually (rather than using a resolver) keeps
    the test independent of ``jsonschema``'s internal resolver API,
    which is not part of its public contract.
    """
    schema = load_schema()
    defs = schema.get("$defs", {})
    unresolved: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in defs:
                    unresolved.append(f"{path} -> {ref}")
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}")

    walk(schema, "#")
    assert not unresolved, "dangling $refs detected:\n  " + "\n  ".join(unresolved)


def test_no_orphan_defs() -> None:
    """Every entry in ``$defs`` must be referenced at least once.

    An unreferenced definition is dead weight: it does not fail any
    test, it does not affect validation, and it drifts out of sync
    with the rest of the schema as the contract evolves. Removing
    orphans *now* is cheap; removing them later requires proving
    no external tooling (a future schema exporter, a documentation
    generator) was consuming them.
    """
    schema = load_schema()
    defs = set(schema.get("$defs", {}))
    referenced: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                referenced.add(ref.removeprefix("#/$defs/"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    orphans = defs - referenced
    assert not orphans, f"orphan $defs entries (declared but never referenced): {orphans}"


# =============================================================================
# Pattern validity
# =============================================================================
def test_all_patterns_compile_under_python_re() -> None:
    """Every ``pattern`` string must compile with Python's ``re``.

    The JSON Schema spec requires *ECMA-262* regex syntax; Python's
    ``re`` is close but not identical. A pattern that is valid ECMA
    but invalid ``re`` (e.g. a lookbehind Python's ``re`` rejects,
    or a group-name syntax Python's ``re`` forbids) would pass
    ``check_schema`` (which only validates structure) but fail the
    first time a *string* is validated against it.

    Catching this here keeps the failure at build time, not at
    ``forge create`` time on a user's laptop.
    """
    schema = load_schema()
    bad_patterns: list[tuple[str, str]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            pattern = node.get("pattern")
            if isinstance(pattern, str):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    bad_patterns.append((f"{path}/pattern", f"{pattern!r}: {exc}"))
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}")

    walk(schema, "#")
    assert not bad_patterns, "patterns that do not compile under re:\n" + "\n".join(
        f"  {path}: {detail}" for path, detail in bad_patterns
    )


# =============================================================================
# Property descriptions
# =============================================================================
def test_every_top_level_property_is_described() -> None:
    """Every top-level property must have a non-empty ``description``.

    The schema *is* the documentation (ADR-0007). A property without
    a description forces a reader to reverse-engineer intent from
    the constraint set — which is exactly the failure mode that
    SCHEMA_REFERENCE.md exists to prevent, and that document is
    generated *from* these descriptions. Missing descriptions make
    the reference incomplete.
    """
    schema = load_schema()
    missing = [
        name
        for name, spec in schema["properties"].items()
        if not isinstance(spec, dict) or not spec.get("description")
    ]
    assert not missing, f"top-level properties without a description: {missing}"


def test_every_def_is_described() -> None:
    """Every ``$defs`` entry must have a non-empty ``description``.

    Same reasoning as the top-level check: these are the shapes a
    blueprint author must understand. An undescribed ``$defs`` entry
    is a black box in the reference documentation.
    """
    schema = load_schema()
    missing = [
        name
        for name, spec in schema.get("$defs", {}).items()
        if not isinstance(spec, dict) or not spec.get("description")
    ]
    # Note: ``$defs`` entries that are pure ``$ref`` aliases
    # legitimately have no description of their own — the referenced
    # definition carries it. We exclude those here so the test asserts
    # the right thing: "each *shape* must be described", not "each
    # *name* must be described".
    missing = [
        name
        for name in missing
        if not isinstance(schema["$defs"][name], dict) or "$ref" not in schema["$defs"][name]
    ]
    assert not missing, f"$defs entries without a description: {missing}"
