"""Discovery works when no local blueprints exist.

Scope
-----
This file exists to answer one specific question: *does discovery
behave sensibly when the user has no `.forge/blueprints/` directory?*

The answer must be "yes, and it is the normal case." Most users never
create a local blueprint; they discover the official ones, run
`forge create`, and never look back. If "no local blueprints" caused
an error, a warning, or a special code path, every first-run user
would hit it — and would have no way to distinguish "the tool is
working normally" from "the tool is broken."

This is acceptance criterion 5 for the discovery task, and it is
tested here rather than as a fixture in other suites because:

* A test that *sets up* "no local directory" and then asserts
  something else (e.g. `test_catalogue_is_sorted`) passes for the
  wrong reason if the local source is accidentally *disabled* instead
  of merely *empty*. Those are different bugs with different fixes,
  and the assertion in this file distinguishes them.
* The behaviour has three sub-cases (no path, no directory, empty
  directory) that need to be exercised independently. Putting them in
  `test_service.py` would grow that file's fixture setup without
  adding value to the tests that actually live there.

What is tested here
-------------------
1. **No path configured** — ``LocalDirectorySource(root=Path("/does/not/exist"))``
   yields nothing, and ``exists()`` is ``False``.
2. **No directory present** — a service constructed with an absent
   root still returns the full official catalogue.
3. **Directory present but empty** — a service whose local root is an
   empty directory returns the same catalogue as (2), with no
   warnings.
4. **Empty catalogue vs empty local layer** — a service whose *both*
   layers are empty returns ``[]`` (not an error), and the CLI's
   "no blueprints found" branch is reachable from the service result.

What is *not* tested here
-------------------------
* Behaviour with a *malformed* local blueprint — that is
  ``test_malformed_skip.py``'s job.
* Precedence between a local and an official blueprint with the same
  id — that is ``test_resolver.py``'s job.
* The CLI's rendering of "no blueprints found" — that is the list
  command's job.

The tests here use the real ``BundledSource`` (via the default
composition) rather than a fake, because the point of the criterion is
that the *default* composition works with no local directory. A test
that substituted sources would prove nothing about the case users
actually hit.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from forge.discovery import BlueprintDiscoveryService
from forge.discovery.sources import Candidate, LocalDirectorySource
from forge.models.blueprint import BlueprintSource


# -----------------------------------------------------------------------------
# LocalDirectorySource — the base case
# -----------------------------------------------------------------------------
class TestLocalDirectorySourceAbsent:
    """The local source degrades to an empty stream when its root is missing."""

    @pytest.mark.unit()
    def test_absent_root_yields_no_candidates(self, tmp_path: Path) -> None:
        """A missing root produces zero candidates, not an exception.

        The user has not created `.forge/blueprints/`. This is not a
        configuration error, a permissions problem, or a typo — it is
        the default state. The source must treat "the directory does
        not exist" the same as "the directory exists and contains
        nothing" from the caller's perspective: no candidates.
        """
        src = LocalDirectorySource(root=tmp_path / "never-created")

        assert list(src.candidates()) == []

    @pytest.mark.unit()
    def test_absent_root_reports_not_exists(self, tmp_path: Path) -> None:
        """``exists()`` returns ``False`` for a missing root.

        The service and the CLI use ``exists()`` as a cheap
        short-circuit before iterating. Returning ``True`` would cause
        a needless ``iterdir()`` on every discovery call; returning
        ``False`` for a directory that *does* exist would silently
        disable the layer. Both directions need explicit coverage.
        """
        src = LocalDirectorySource(root=tmp_path / "never-created")

        assert src.exists() is False

    @pytest.mark.unit()
    def test_empty_root_directory_yields_no_candidates(self, tmp_path: Path) -> None:
        """An empty directory produces zero candidates.

        The user created `.forge/blueprints/` but has not put anything
        in it yet. Same outcome as (1), but a different code path —
        ``exists()`` is ``True``, ``iterdir()`` yields nothing, and
        the loop exits without producing a candidate. A test that
        only covered the absent case would leave this branch
        untested.
        """
        src = LocalDirectorySource(root=tmp_path)

        assert list(src.candidates()) == []

    @pytest.mark.unit()
    def test_empty_root_directory_reports_exists(self, tmp_path: Path) -> None:
        """``exists()`` returns ``True`` for an empty directory.

        The existence of the directory and the existence of blueprints
        inside it are independent facts. Conflating them would make
        ``exists()`` re-scan the directory on every call — the cheap
        check would no longer be cheap.
        """
        src = LocalDirectorySource(root=tmp_path)

        assert src.exists() is True


# -----------------------------------------------------------------------------
# Service composition — with real sources
# -----------------------------------------------------------------------------
class TestServiceWithNoLocalLayer:
    """The service works with an absent local layer using real sources.

    These tests deliberately use ``BlueprintDiscoveryService`` with its
    default bundled source, not a fake. The point of the acceptance
    criterion is that the *default composition* behaves correctly
    when the user has no local directory. A fake bundled source would
    prove nothing about the case users actually hit — it would prove
    only that the service can compose a fake with an empty local
    source, which is not the question.
    """

    @pytest.mark.unit()
    def test_absent_local_root_still_yields_official_catalogue(self, tmp_path: Path) -> None:
        """The official catalogue loads unchanged when the local root is missing.

        The only thing the absent local root should change is the
        *source* tag on blueprints: everything should be
        ``BlueprintSource.OFFICIAL``. If a refactor accidentally
        skipped official loading when the local layer failed, this
        would catch it — the catalogue would be empty instead of
        official-tagged.
        """
        service = BlueprintDiscoveryService(local_root=tmp_path / "never-created")

        catalogue = service.discover()

        # The official catalogue is non-empty in any installable wheel.
        # An empty result here means the bundled source was not
        # consulted — a real bug, not a test setup problem.
        assert catalogue, "official catalogue is empty — is the bundled package installed?"
        assert all(bp.source is BlueprintSource.OFFICIAL for bp in catalogue)

    @pytest.mark.unit()
    def test_empty_local_directory_still_yields_official_catalogue(self, tmp_path: Path) -> None:
        """Same as above, but with an existing-and-empty local directory.

        The ``exists()`` check for this case returns ``True``, so the
        source iterates and yields nothing. If a refactor broke the
        "iterate empty directory" path (say, by treating a zero-length
        directory listing as an error), the official catalogue would
        disappear — a bug that the absent-directory test above would
        miss.
        """
        service = BlueprintDiscoveryService(local_root=tmp_path)

        catalogue = service.discover()

        assert catalogue, "official catalogue is empty — is the bundled package installed?"
        assert all(bp.source is BlueprintSource.OFFICIAL for bp in catalogue)

    @pytest.mark.unit()
    def test_absent_local_root_produces_no_warnings(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No local directory is normal — it does not warn.

        A warning here would train users to ignore warnings. The
        "malformed local blueprint" case is worth warning about (the
        user did something wrong); "you have not configured local
        blueprints" is the default state of every fresh install. The
        resolver's warning path must not fire when the *source*
        produced no candidates — only when it produced a candidate
        that turned out to be malformed.
        """
        service = BlueprintDiscoveryService(local_root=tmp_path / "never-created")

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            service.discover()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [], (
            "no local directory is the default state; it must not "
            f"produce warnings, got: {[w.getMessage() for w in warnings]}"
        )

    @pytest.mark.unit()
    def test_empty_local_directory_produces_no_warnings(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty local directory also produces no warnings.

        Same principle as above. The user may have created the
        directory in preparation for adding a blueprint; an empty
        state is not a mistake.
        """
        service = BlueprintDiscoveryService(local_root=tmp_path)

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            service.discover()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []


# -----------------------------------------------------------------------------
# Both layers empty — the catalogue is empty, not broken
# -----------------------------------------------------------------------------
class TestBothLayersEmpty:
    """When neither layer produces candidates, ``discover()`` returns ``[]``.

    An empty catalogue is a valid answer to a well-formed question. The
    CLI's ``list`` command renders a helpful "no blueprints found"
    message for this case; if the service raised an exception instead,
    that message would never be reached and the user would see a
    traceback. The distinction between "empty catalogue" and "discovery
    failure" is a contract the CLI depends on.
    """

    @pytest.mark.unit()
    def test_discover_returns_empty_list_not_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The service returns ``[]`` when every source yields nothing.

        Uses an injected source list to simulate "no bundled and no
        local," which is the state a broken installation would produce
        — or a deliberate configuration that ships no official
        blueprints. The service's job in that case is to return an
        empty catalogue, not to raise. If a future refactor added a
        ``raise DiscoveryError("no blueprints")`` in that branch, this
        test fails.
        """

        class _EmptySource:
            def candidates(self) -> iter[Candidate]:
                return iter(())

        service = BlueprintDiscoveryService(sources=[_EmptySource()])

        catalogue = service.discover()

        assert catalogue == []

    @pytest.mark.unit()
    def test_load_raises_discovery_error_for_missing_id_in_empty_catalogue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load()`` distinguishes "empty catalogue" from "unknown id".

        The service returns ``[]`` for ``discover()`` but raises for
        ``load("anything")``. Those are *different* contracts: an
        empty list is a valid answer to "list what you have"; a
        ``DiscoveryError`` is a valid answer to "give me this one
        specifically and it is not there." Conflating them would either
        make ``discover`` raise (breaking the CLI's empty-catalogue
        message) or make ``load`` return ``None`` (breaking the
        ``Blueprint`` return type).
        """
        from forge.core.exceptions import DiscoveryError

        class _EmptySource:
            def candidates(self) -> iter[Candidate]:
                return iter(())

        service = BlueprintDiscoveryService(sources=[_EmptySource()])

        with pytest.raises(DiscoveryError) as info:
            service.load("python.fastapi.api")

        # The empty-catalogue branch of the error message is
        # distinct from the "not found in a non-empty catalogue"
        # branch — the user's fix is different (fix the install vs.
        # fix the id). Asserting on the recovery phrase here pins
        # that distinction.
        assert "forge doctor" in str(info.value) or "installed" in str(info.value)
