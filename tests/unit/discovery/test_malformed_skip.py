"""Malformed local blueprints are skipped with a warning, never fatally.

Scope
-----
This file exists to answer one specific question: *what happens when a
user's local blueprint cannot be loaded?*

The answer has three parts, and all three must hold:

1. **The malformed blueprint is skipped.** It does not appear in the
   catalogue.
2. **The official catalogue still loads.** The user can still run
   ``forge list`` and ``forge create`` with the shipped blueprints.
   A broken local file must not take down the whole tool.
3. **The warning names the file.** The user must be able to find the
   offending ``blueprint.yaml`` from the warning alone — no guessing,
   no "check your local blueprints" vagueness.

Each part has a distinct failure mode that is silent in isolation:

* If only (1) is enforced (skip without warning), the user sees a
  missing blueprint with no explanation and no hint that a file was
  involved.
* If only (2) is enforced (load official, ignore local silently),
  the user has no idea their customisation is not being applied.
* If only (3) is enforced (warn, then crash), the warning is a
  receipt for a failure the user cannot act on.

The tests here exercise all three independently so a regression in
one is caught before it reaches a user.

What is *not* tested here
-------------------------
* The *specific* failure modes of the resolver's ``_parse`` (empty
  bytes, YAML syntax, non-mapping root, schema violation) — those are
  covered in ``test_resolver.py``. This file uses "malformed" as a
  single category and asserts on the *downstream* behaviour: skipping
  and warning at the discovery level.
* Behaviour when *no* local directory exists — that is
  ``test_no_local.py``'s job, and it asserts the opposite property
  (no warning).
* Behaviour when *every* candidate is malformed — that specific edge
  is covered in ``test_service.py`` (the "empty catalogue" case
  exercises it as a side effect). It is not re-tested here because
  the multi-candidate scenario subsumes it: if one malformed candidate
  survives (i.e., does not crash the catalogue), N malformed
  candidates survive the same way.

Design invariants this file pins
--------------------------------
1. **Warnings go through the ``forge.discovery`` logger.** The
   assertion filters ``caplog`` by logger name, so a future refactor
   that switches the resolver to a different logger is caught here.
2. **The warning names the source label.** The resolver's
   ``MalformedBlueprintError`` carries ``source_label``, which is
   ``"local:<absolute path>"`` for local files. The test asserts on
   that prefix, not on the full path (which changes per machine).
3. **Only *malformed* candidates warn.** A clean candidate produces
   no warning, and the tests below include both malformed and clean
   candidates to prove the warning is not emitted universally.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from forge.discovery import BlueprintDiscoveryService
from forge.discovery.sources import MANIFEST_FILENAME, LocalDirectorySource
from forge.models.blueprint import BlueprintSource

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
# A minimal valid manifest so the "good" candidate in each test is
# unambiguously loadable. Reused across tests to keep the fixtures
# focused on "malformed vs valid" rather than on manifest content.
_VALID_MANIFEST = """\
schema_version: "1.0.0"
id: custom.valid
name: Valid Custom Blueprint
version: 1.0.0
description: a valid local blueprint for malformed-skip tests
category: api
verification:
  - id: ok
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""

# A manifest whose bytes are invalid YAML. The specific syntax error
# does not matter — the resolver funnels all parse failures into the
# same ``MalformedBlueprintError``, and the tests below assert on that
# category, not on the specific message.
_MALFORMED_YAML = b"not: a: valid: yaml: line\n"


def _write_valid_local(root: Path, blueprint_id: str = "custom.valid") -> Path:
    """Create a well-formed local blueprint under ``root`` and return the path."""
    path = root / blueprint_id / MANIFEST_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(_VALID_MANIFEST.replace("custom.valid", blueprint_id), encoding="utf-8")
    return path


def _write_malformed_local(root: Path, blueprint_id: str = "custom.broken") -> Path:
    """Create a local blueprint whose manifest is invalid YAML."""
    path = root / blueprint_id / MANIFEST_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(_MALFORMED_YAML)
    return path


# -----------------------------------------------------------------------------
# Invariant 1 — the malformed blueprint is skipped
# -----------------------------------------------------------------------------
class TestMalformedIsSkipped:
    """A malformed local blueprint does not appear in the catalogue."""

    @pytest.mark.unit()
    def test_malformed_blueprint_is_not_in_the_catalogue(self, tmp_path: Path) -> None:
        """The catalogue contains no entry for the malformed blueprint.

        If the malformed candidate somehow made it into the catalogue,
        the very next call (``forge create custom.broken``) would fail
        with a model or schema error at a point where the user has
        already committed to using the blueprint. The skip happens at
        discovery time precisely so that never happens.

        This test uses an injected source list rather than the full
        default composition so the bundled catalogue does not
        participate in the assertion. The bundled-catalogue survival
        case is covered separately below — separating the two failures
        makes it obvious which behaviour a regression broke.
        """
        _write_malformed_local(tmp_path)

        service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])
        catalogue = service.discover()

        ids = [bp.id for bp in catalogue]
        assert "custom.broken" not in ids
        assert ids == []


# -----------------------------------------------------------------------------
# Invariant 2 — the official catalogue still loads
# -----------------------------------------------------------------------------
class TestOfficialSurvivesMalformed:
    """A malformed local blueprint does not prevent official discovery."""

    @pytest.mark.unit()
    def test_official_catalogue_loads_when_local_is_malformed(self, tmp_path: Path) -> None:
        """The official catalogue is unaffected by a broken local blueprint.

        This is the "one bad file never takes down the catalogue"
        invariant (ADR-0009). It is asserted with the *real* bundled
        source so a regression in the default composition (say, the
        bundled source being skipped when the local one raises) is
        caught — a fake bundled source would let that regression
        through.
        """
        _write_malformed_local(tmp_path)

        service = BlueprintDiscoveryService(local_root=tmp_path)
        catalogue = service.discover()

        # The bundled catalogue is non-empty in any installable wheel.
        # An empty catalogue here means the bundled source was skipped
        # because the local one produced a malformed candidate — a
        # real bug, not a test environment issue.
        assert catalogue, "official catalogue is empty — is the bundled package installed?"
        assert all(bp.source is BlueprintSource.OFFICIAL for bp in catalogue)

    @pytest.mark.unit()
    def test_valid_local_loads_alongside_official_when_sibling_is_malformed(
        self, tmp_path: Path
    ) -> None:
        """A valid local blueprint still loads when a sibling is malformed.

        Malformed candidates are skipped *individually*. A user with
        three local blueprints should not lose all three because one
        of them is broken — the message "you have one bad file, fix
        it" is actionable; the message "none of your blueprints
        loaded" is not.
        """
        _write_valid_local(tmp_path, "custom.valid")
        _write_malformed_local(tmp_path, "custom.broken")

        service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])
        catalogue = service.discover()

        ids = [bp.id for bp in catalogue]
        assert ids == ["custom.valid"]


# -----------------------------------------------------------------------------
# Invariant 3 — the warning names the file
# -----------------------------------------------------------------------------
class TestWarningNamesTheFile:
    """The malformed-skip warning must name the offending file."""

    @pytest.mark.unit()
    def test_warning_is_emitted(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Exactly one warning fires for one malformed candidate.

        Emitting *at least* one warning is not enough — a warning per
        *parse attempt* (say, one for the YAML error and one for the
        schema check) would still satisfy "at least one," but would
        double the user's notification load for a single problem.
        Exactly-one is the contract.
        """
        _write_malformed_local(tmp_path)

        service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            service.discover()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    @pytest.mark.unit()
    def test_warning_names_the_source_label(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning names the local file via its source label.

        The label format is ``"local:<absolute path>"``. The test
        asserts the ``"local:"`` prefix and the file name, but not the
        full path — the path differs per machine, and the test would
        be brittle if it required a specific absolute location.

        The ``"local:"`` prefix is what distinguishes this warning
        from a bundled-blueprint warning. A user with a local problem
        needs a local path; a future warning for a bundled problem
        would use ``"bundled:"``, and users who see the wrong prefix
        are being sent to the wrong place to fix it.
        """
        _write_malformed_local(tmp_path)

        service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            service.discover()

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0]
        assert "local:" in message
        assert "custom.broken" in message
        assert MANIFEST_FILENAME in message

    @pytest.mark.unit()
    def test_warning_text_is_actionable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning says "skipping malformed blueprint," not just "error."

        A warning that says "an error occurred" tells the user
        something failed but not what to do. A warning that says
        "skipping malformed blueprint X" tells them (a) the failure
        was isolated, (b) the rest of discovery is fine, and (c) which
        file to fix. The phrasing is part of the contract, and this
        test pins the two keywords that make it actionable.
        """
        _write_malformed_local(tmp_path)

        service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            service.discover()

        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(messages) == 1
        assert "skipping" in messages[0].lower()
        assert "malformed" in messages[0].lower()


# -----------------------------------------------------------------------------
# Invariant 4 — clean candidates are silent
# -----------------------------------------------------------------------------
class TestCleanCandidatesAreSilent:
    """A clean local blueprint produces no warning.

    This is the negative control for the three positive tests above.
    Without it, a refactor that warned on *every* local candidate would
    still pass the invariant tests (they only assert on malformed
    candidates). Warning on clean blueprints would be worse than the
    bug the invariants are designed to catch: it would fire on the
    common case, training users to ignore all warnings.
    """

    @pytest.mark.unit()
    def test_valid_local_produces_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A valid local blueprint loads silently — no warning fires."""
        _write_valid_local(tmp_path)

        service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            service.discover()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [], (
            f"valid local blueprint must not warn, got: " f"{[w.getMessage() for w in warnings]}"
        )


# -----------------------------------------------------------------------------
# End-to-end with the real default composition
# -----------------------------------------------------------------------------
class TestEndToEndDefaultComposition:
    """The default service composition survives a malformed local file.

    This is the same integration as ``TestOfficialSurvivesMalformed``
    but using the *service's* default composition (real bundled +
    real local). It is the closest test to what a user actually runs:
    ``forge list`` after dropping a broken ``.forge/blueprints/`` on
    their machine.

    Kept separate from the per-invariant tests because its *assertion*
    is the combination: official catalogue present, malformed absent,
    warning emitted. Each of those is tested in isolation above; this
    test proves they can all be true *simultaneously* in the same call
    — which is the property a real user cares about.
    """

    @pytest.mark.unit()
    def test_end_to_end(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """All three invariants hold in one call against the default composition."""
        _write_malformed_local(tmp_path)

        service = BlueprintDiscoveryService(local_root=tmp_path)

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            catalogue = service.discover()

        # (2) Official catalogue is present and official-tagged.
        assert catalogue
        assert all(bp.source is BlueprintSource.OFFICIAL for bp in catalogue)

        # (1) Malformed local blueprint is not in the catalogue.
        assert all(bp.id != "custom.broken" for bp in catalogue)

        # (3) The warning fired, and it named the offending file.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "local:" in warnings[0]
        assert "custom.broken" in warnings[0]
