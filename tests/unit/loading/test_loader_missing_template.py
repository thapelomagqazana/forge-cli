"""Unit tests for `BlueprintLoader.load` on missing-template references.

What this suite must prove
--------------------------
The loader has four phases — YAML parse, schema, model, filesystem — and
the filesystem phase is the only one that produces errors a *schema-
valid* document can have. That makes it the phase most likely to be
silently dropped in a refactor: an author looking at the schema and
model tests, which both accept the same fixture set, could conclude
that "the loader validates the document" and remove the filesystem
phase as redundant.

This suite exists to prevent exactly that. It proves four properties:

1. **Missing templates are errors, not crashes.** The loader's
   contract is `Result[Blueprint, list[ValidationError]]`, and a
   blueprint whose `files[].source` or `conditional_files[].files[].source`
   references a path that does not exist returns `Err([...])`, not a
   raised `OSError` or an `Ok` that fails later during rendering.

2. **Missing templates aggregate.** A blueprint that references
   three missing templates produces three errors, not one.

3. **Missing templates are located precisely.** Each error's path
   names the *entry* whose template is missing.

4. **``check_files=False`` disables the phase without altering the
   rest of the pipeline.** This is what discovery uses.

A note on schema-valid identifiers
----------------------------------
The schema's `id` pattern is
`^[a-z][a-z0-9]*(\\.[a-z][a-z0-9]*)+$` — no hyphens, no underscores
inside a segment. Every inline fixture in this file must use a
schema-valid id, or the schema phase will reject the document before
the filesystem phase ever runs, and the test will fail with a
*schema* error that looks nothing like the *missing-template* error
it was written to exercise. The `_blueprint` helper below builds
bodies from schema-valid ids (hyphens are stripped by convention,
not by the helper — do not reintroduce them in inline bodies).

What this suite is *not* trying to prove
----------------------------------------
* **Path-traversal defence.** `fs_checks.py` includes a containment
  check; its tests live with the containment logic, not here.

* **Readability.** A template that exists but is mode `000` is a
  distinct OS-level condition; such a test is fragile across
  platforms and belongs with the containment tests.

* **Rendering.** Whether the template's content is valid Jinja is a
  rendering concern. The loader only checks existence and readability.

Fixture strategy
----------------
The shared `tests/fixtures/loader/missing-template.yaml` file
references a template (`templates/does-not-exist.j2`) that must
*not* exist. The other tests build their blueprints inline into a
per-test temp directory, which gives full control over the filesystem
shape.

The partial-filesystem test (some templates present, some missing)
is the interesting case because it is what users actually encounter:
a blueprint where the `templates/` directory was copied but one file
was not.
"""

from __future__ import annotations

from pathlib import Path

from forge.loading import BlueprintLoader, Err, Ok

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "loader"
_MISSING_TEMPLATE_FIXTURE = _FIXTURES / "missing-template.yaml"


# =============================================================================
# Helpers
# =============================================================================
def _write_blueprint(root: Path, body: str) -> Path:
    """Write ``body`` as ``root/blueprint.yaml`` and return the path."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "blueprint.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _touch(root: Path, relpath: str) -> None:
    """Create an empty file at ``root/relpath``, including parents.

    Templates are only checked for existence and readability by the
    loader, so empty files are sufficient. A helper that wrote
    "realistic" template content would be misleading (implying
    content matters) and slow.
    """
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")


# =============================================================================
# The shared fixture — a canonical "one missing template"
# =============================================================================
class TestSharedMissingTemplateFixture:
    """The shared fixture must produce `Err` with a `files.0.source` path.

    `missing-template.yaml` is deliberately minimal: one `files:` entry
    whose template is absent. Its `id:` is `sample.missingtemplate`
    (no hyphens) because the schema's identifier pattern forbids them
    inside a segment — see the module docstring's note on
    schema-valid identifiers.
    """

    def test_shared_fixture_is_err(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)

    def test_shared_fixture_reports_missing_template(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        assert any(
            "missing template" in e.message.lower() for e in result.error
        ), f"no 'missing template' in {[e.message for e in result.error]!r}"

    def test_shared_fixture_error_path_names_the_entry(self):
        """The path is ``files.0.source``, not just ``files`` or a phase
        prefix."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        paths = [e.path for e in result.error]
        assert any(
            "files.0.source" in p for p in paths
        ), f"expected path 'files.0.source' in {paths}"

    def test_shared_fixture_error_names_the_missing_file(self):
        """The message names the relative path the fixture references."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        assert any(
            "does-not-exist.j2" in e.message for e in result.error
        ), f"missing filename not in {[e.message for e in result.error]!r}"

    def test_shared_fixture_error_has_hint(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        for error in result.error:
            assert error.hint, f"empty hint on {error.path!r}"


# =============================================================================
# Aggregation — multiple missing templates
# =============================================================================
class TestMultipleMissingTemplates:
    """A blueprint with three missing templates reports three errors.

    The inline fixture's id is `sample.threemissing` — hyphen removed
    because the schema's identifier pattern forbids it inside a
    segment. A hyphenated id would fail the *schema* phase before the
    filesystem phase ran, and the test would report a schema error
    rather than the missing-template errors it is named after.
    """

    _BODY = """
schema_version: "1.0.0"
id: sample.threemissing
name: Three Missing Templates
version: "1.0.0"
description: d
category: api
files:
  - source: templates/a.j2
    destination: a.txt
  - source: templates/b.j2
    destination: b.txt
  - source: templates/c.j2
    destination: c.txt
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""

    def test_three_missing_templates_produce_three_errors(self, tmp_path: Path):
        result = BlueprintLoader().load(_write_blueprint(tmp_path, self._BODY))
        assert isinstance(result, Err)
        assert len(result.error) == 3

    def test_each_missing_template_is_named(self, tmp_path: Path):
        result = BlueprintLoader().load(_write_blueprint(tmp_path, self._BODY))
        assert isinstance(result, Err)
        messages = "\n".join(e.message for e in result.error)
        for name in ("templates/a.j2", "templates/b.j2", "templates/c.j2"):
            assert name in messages, f"{name} not reported in {messages!r}"

    def test_each_missing_template_has_a_distinct_path(self, tmp_path: Path):
        """The three errors point at ``files.{0,1,2}.source`` — distinct paths.

        A regression that collapsed them into one error would pass
        "three errors" only if the count came from somewhere else;
        asserting distinct paths pins "one error per entry".
        """
        result = BlueprintLoader().load(_write_blueprint(tmp_path, self._BODY))
        assert isinstance(result, Err)
        paths = {e.path for e in result.error}
        assert paths == {
            "files.0.source",
            "files.1.source",
            "files.2.source",
        }, f"expected three distinct entry paths, got {paths}"


# =============================================================================
# Partial filesystem — some templates exist, some do not
# =============================================================================
class TestPartialFilesystem:
    """The most realistic case: some templates present, some missing.

    A user who deletes a template, or who copies a blueprint without
    its `templates/` directory, produces exactly this shape. The loader
    must report only the missing ones.
    """

    def test_only_missing_templates_are_reported(self, tmp_path: Path):
        body = """
schema_version: "1.0.0"
id: sample.partial
name: Partial Filesystem
version: "1.0.0"
description: d
category: api
files:
  - source: templates/present.j2
    destination: present.txt
  - source: templates/missing.j2
    destination: missing.txt
  - source: templates/also-present.j2
    destination: also-present.txt
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        blueprint = _write_blueprint(tmp_path, body)
        _touch(tmp_path, "templates/present.j2")
        _touch(tmp_path, "templates/also-present.j2")
        # templates/missing.j2 is deliberately not created.

        result = BlueprintLoader().load(blueprint)
        assert isinstance(result, Err)
        assert len(result.error) == 1
        assert "missing.j2" in result.error[0].message

    def test_all_templates_present_is_ok(self, tmp_path: Path):
        """The complement: with every template present, the load is `Ok`.

        Without this test, the partial case would pass on a loader
        that always errors when any template is declared — because
        the partial case has at least one missing. This pins the
        success case at the same shape.
        """
        body = """
schema_version: "1.0.0"
id: sample.complete
name: Complete Filesystem
version: "1.0.0"
description: d
category: api
files:
  - source: templates/a.j2
    destination: a.txt
  - source: templates/b.j2
    destination: b.txt
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        blueprint = _write_blueprint(tmp_path, body)
        _touch(tmp_path, "templates/a.j2")
        _touch(tmp_path, "templates/b.j2")

        result = BlueprintLoader().load(blueprint)
        assert isinstance(result, Ok), (
            f"expected Ok, got Err: " f"{getattr(result, 'error', '<no error>')}"
        )


# =============================================================================
# Conditional files
# =============================================================================
class TestConditionalFiles:
    """Conditional file references are checked, and their paths nest.

    A conditional file that *would* be used when the condition fires
    must exist — the blueprint's author committed to it as part of
    the deliverable. Deferring the check to condition-evaluation time
    would push a load error into the rendering phase.
    """

    def test_missing_conditional_file_is_reported(self, tmp_path: Path):
        body = """
schema_version: "1.0.0"
id: sample.conditional
name: Conditional
version: "1.0.0"
description: d
category: api
files: []
conditional_files:
  - condition: include_docker == true
    files:
      - source: templates/Dockerfile.j2
        destination: Dockerfile
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = BlueprintLoader().load(_write_blueprint(tmp_path, body))
        assert isinstance(result, Err)
        assert len(result.error) == 1
        # The path nests: conditional_files.0.files.0.source.
        assert result.error[0].path == "conditional_files.0.files.0.source"

    def test_conditional_file_present_is_ok(self, tmp_path: Path):
        """Present conditional files do not cause errors."""
        body = """
schema_version: "1.0.0"
id: sample.conditional
name: Conditional
version: "1.0.0"
description: d
category: api
files: []
conditional_files:
  - condition: include_docker == true
    files:
      - source: templates/Dockerfile.j2
        destination: Dockerfile
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        blueprint = _write_blueprint(tmp_path, body)
        _touch(tmp_path, "templates/Dockerfile.j2")

        result = BlueprintLoader().load(blueprint)
        assert isinstance(result, Ok)

    def test_static_and_conditional_errors_aggregate(self, tmp_path: Path):
        """Static and conditional missing templates appear in one report."""
        body = """
schema_version: "1.0.0"
id: sample.mixed
name: Mixed
version: "1.0.0"
description: d
category: api
files:
  - source: templates/static.j2
    destination: static.txt
conditional_files:
  - condition: include_docker == true
    files:
      - source: templates/Dockerfile.j2
        destination: Dockerfile
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = BlueprintLoader().load(_write_blueprint(tmp_path, body))
        assert isinstance(result, Err)
        assert len(result.error) == 2
        paths = {e.path for e in result.error}
        assert "files.0.source" in paths
        assert "conditional_files.0.files.0.source" in paths


# =============================================================================
# check_files=False — the discovery option
# =============================================================================
class TestCheckFilesOptOut:
    """``check_files=False`` skips the filesystem phase only.

    Discovery uses this option because in-memory candidates are staged
    into temp directories whose templates cannot exist by
    construction. The option must skip the filesystem phase without
    weakening schema or model validation.
    """

    def test_missing_template_is_ok_with_check_files_false(self, tmp_path: Path):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE, check_files=False)
        assert isinstance(result, Ok), (
            f"expected Ok with check_files=False, got Err: "
            f"{getattr(result, 'error', '<no error>')}"
        )

    def test_schema_error_still_fails_with_check_files_false(self, tmp_path: Path):
        """The opt-out must not become a "skip all validation" flag.

        A regression that made `check_files=False` also skip schema
        validation would let discovery accept documents that
        `forge validate` rejects — the two commands would silently
        diverge.
        """
        body = """
schema_version: "1.0.0"
id: sample.badversion
name: Bad Version
version: "1.0"
description: d
category: api
files: []
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = BlueprintLoader().load(_write_blueprint(tmp_path, body), check_files=False)
        assert isinstance(result, Err)
        assert any(
            e.path.endswith("version") for e in result.error
        ), f"expected version error, got {[e.path for e in result.error]}"

    def test_model_error_still_fails_with_check_files_false(self, tmp_path: Path):
        """Same for the model phase — the opt-out is scoped to filesystem.

        Uses a schema-caught violation (`type: integer` fails the
        schema's enum). A true model-only violation requires a
        schema-model parity bug to construct, which would be a
        different failure than this test intends; the model parity
        suite covers that case.
        """
        body = """
schema_version: "1.0.0"
id: sample.badtype
name: Bad Type
version: "1.0.0"
description: d
category: api
files: []
variables:
  - id: bad
    type: integer
    description: d
    required: true
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = BlueprintLoader().load(_write_blueprint(tmp_path, body), check_files=False)
        assert isinstance(result, Err)
        assert any(
            "variables" in e.path and e.path.endswith("type") for e in result.error
        ), f"expected variables.0.type error, got {[e.path for e in result.error]}"


# =============================================================================
# Error content — actionable messages and hints
# =============================================================================
class TestErrorActionability:
    """Missing-template errors carry location, path, and hint."""

    def test_error_mentions_the_missing_file(self, tmp_path: Path):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        assert any("does-not-exist.j2" in e.message for e in result.error)

    def test_error_has_hint_naming_the_fix(self, tmp_path: Path):
        """The hint names the fix — copy or create the file, or remove
        the reference."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        # Find the missing-template error specifically — the hint on
        # the schema error (if any) is unrelated.
        missing = [e for e in result.error if "does-not-exist.j2" in e.message]
        assert missing, f"no missing-template error in {[e.message for e in result.error]!r}"
        hint = missing[0].hint.lower()
        assert (
            "copy" in hint or "create" in hint or "remove" in hint
        ), f"hint does not name a fix: {missing[0].hint!r}"

    def test_error_source_is_the_blueprint_file(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        expected = str(_MISSING_TEMPLATE_FIXTURE)
        for error in result.error:
            assert error.source == expected

    def test_rendered_error_is_copy_pasteable(self):
        """`ValidationError.format()` produces one line naming the
        source, the path, the message, and the hint."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        missing = [e for e in result.error if "does-not-exist.j2" in e.message]
        assert missing
        rendered = missing[0].format()
        assert str(_MISSING_TEMPLATE_FIXTURE) in rendered
        assert "files.0.source" in rendered
        assert "hint:" in rendered.lower()
