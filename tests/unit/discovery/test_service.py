"""Unit tests for ``forge.discovery.service.BlueprintDiscoveryService``.

Scope
-----
The service is an orchestrator: it composes sources, hands their
candidates to the resolver, and projects the result. It deliberately
owns no policy. These tests therefore assert on *orchestration
contracts* — ordering, deduplication, source injection, error
translation — not on the specific rules the resolver or sources apply.
Those rules have their own test suites:

    * precedence and warnings — ``test_resolver.py``
    * candidate streaming       — ``test_sources.py``
    * malformed-skip behaviour  — ``test_malformed_skip.py``
    * no-local behaviour        — ``test_no_local.py``

The seam every test here uses is ``BlueprintDiscoveryService(sources=
[...])``: injecting a source list replaces the default composition,
which is the same seam production code will use if a future
``--blueprint-dir`` flag lands. Testing through that seam means the
tests exercise the real composition path, not a mock of it — a
distinction that matters: a test that replaces ``_iter_candidates``
directly would pass even if ``_resolved_sources`` were broken, which
is exactly the bug a service-level test is supposed to catch.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from forge.core.exceptions import DiscoveryError
from forge.discovery import DEFAULT_LOCAL_ROOT, BlueprintDiscoveryService
from forge.discovery.sources import Candidate
from forge.models import Blueprint
from forge.models.blueprint import BlueprintSource, DiscoveredBlueprint

# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------
# A minimal valid manifest. Shared by every fixture below so a schema
# change only has to update one string, and so the tests stay focused
# on orchestration rather than on "is this document valid?" — that
# question has its own suite (``tests/unit/schemas``).
_VALID_MANIFEST_TEMPLATE: Final[str] = """\
schema_version: "1.0.0"
id: {id}
name: {name}
version: 1.0.0
description: fixture blueprint for service tests
category: api
verification:
  - id: ok
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""


def _manifest(blueprint_id: str, name: str = "Fixture") -> bytes:
    """Return a valid manifest as UTF-8 bytes."""
    return _VALID_MANIFEST_TEMPLATE.format(id=blueprint_id, name=name).encode("utf-8")


class _FakeSource:
    """A minimal source that yields a fixed, caller-supplied candidate list.

    Implements only ``candidates()`` — the structural contract the
    service relies on (``SourceLike`` is a union alias, not a base
    class, so any object with the right method is a valid source).
    Using a fake rather than a real ``BundledSource``/``LocalDirectory
    Source`` keeps these tests independent of filesystem layout and of
    the bundled content, which changes independently of the service.
    """

    def __init__(self, candidates: list[Candidate]) -> None:
        # Defensive copy: a test that yields an iterator of the same
        # list twice (as the service does for ``discover`` followed by
        # ``load``) would otherwise consume it once and mysteriously
        # return nothing on the second call. Copying makes the fake
        # behave like a real source (each ``candidates()`` call is
        # independent), which is the contract the service relies on.
        self._candidates = list(candidates)

    def candidates(self) -> Iterator[Candidate]:
        yield from self._candidates


def _service(candidates: list[Candidate]) -> BlueprintDiscoveryService:
    """Construct a service backed by a single fake source.

    Passing ``sources=[...]`` bypasses ``default_sources`` entirely,
    so the test does not need to monkeypatch ``local_root`` or create
    a temporary directory. That keeps the tests fast and makes the
    "which sources did this test use?" question answerable by reading
    the test body, not by tracing through fixture inheritance.
    """
    return BlueprintDiscoveryService(sources=[_FakeSource(candidates)])


# -----------------------------------------------------------------------------
# discover() — happy path
# -----------------------------------------------------------------------------
def test_discover_returns_sorted_catalogue() -> None:
    """The catalogue is sorted by id, regardless of source order.

    The service delegates sorting to ``resolver.to_metadata``, which
    is the documented contract (ADR-0009). Asserting it here means a
    future change to the resolver's sort behaviour is caught at the
    service boundary, where the CLI actually depends on it.
    """
    service = _service(
        [
            ("bundled:zeta", _manifest("zeta.one"), BlueprintSource.OFFICIAL),
            ("bundled:alpha", _manifest("alpha.one"), BlueprintSource.OFFICIAL),
            ("bundled:mid", _manifest("mid.one"), BlueprintSource.OFFICIAL),
        ]
    )

    result = service.discover()

    assert [bp.id for bp in result] == ["alpha.one", "mid.one", "zeta.one"]


def test_discover_returns_discovered_blueprint_views() -> None:
    """The return type is the view, not the full ``Blueprint`` document.

    ``forge list`` relies on this: the view carries only the fields a
    catalogue needs, so the listing command never has to know about
    templates or verification commands. A future refactor that returns
    full documents would still *work* (same fields are present), but
    it would silently couple the listing command to the model layer.
    Asserting the concrete type catches that change at review time.
    """
    service = _service([("bundled:x", _manifest("python.fastapi.api"), BlueprintSource.OFFICIAL)])

    result = service.discover()

    assert len(result) == 1
    assert isinstance(result[0], DiscoveredBlueprint)
    # The view must not carry a ``verification`` attribute; if it did,
    # a caller could start depending on it and the "views stay stable"
    # invariant would be lost.
    assert not hasattr(result[0], "verification")


def test_discover_with_empty_source_returns_empty_list() -> None:
    """An empty source is a valid catalogue — not an error.

    The service must not raise on "no blueprints". The CLI's ``list``
    command renders a helpful message for the empty case; if the
    service raised instead, that message would never be reached and
    the user would see a traceback.
    """
    service = _service([])

    assert service.discover() == []


# -----------------------------------------------------------------------------
# discover() — deduplication
# -----------------------------------------------------------------------------
def test_discover_deduplicates_by_id_with_local_winning() -> None:
    """Duplicate ids collapse to one entry; local beats official.

    This mirrors the resolver's precedence rule (ADR-0009). Testing
    it through the service confirms that the service is not
    accidentally bypassing the resolver — e.g., by yielding raw
    candidates or by concatenating source outputs without deduping.
    """
    service = _service(
        [
            ("bundled:x", _manifest("python.fastapi.api", "Official"), BlueprintSource.OFFICIAL),
            ("local:x", _manifest("python.fastapi.api", "Local"), BlueprintSource.LOCAL),
        ]
    )

    result = service.discover()

    assert len(result) == 1
    assert result[0].id == "python.fastapi.api"
    assert result[0].source is BlueprintSource.LOCAL
    assert result[0].name == "Local"


def test_discover_skips_malformed_candidates_silently() -> None:
    """A malformed candidate does not prevent the catalogue from loading.

    The warning path is covered in ``test_malformed_skip.py``; here we
    only assert that the service survives — the warning assertion
    lives in its own test so a change to the warning format does not
    need to update this one.
    """
    service = _service(
        [
            ("local:bad", b"not: a: mapping: at: the: root", BlueprintSource.LOCAL),
            ("bundled:ok", _manifest("ok.one"), BlueprintSource.OFFICIAL),
        ]
    )

    result = service.discover()

    assert [bp.id for bp in result] == ["ok.one"]


# -----------------------------------------------------------------------------
# load() — happy path
# -----------------------------------------------------------------------------
def test_load_returns_typed_blueprint() -> None:
    """``load()`` returns the full typed document, not the view.

    Downstream consumers (the wizard, the validator, the generator)
    need the full document — variables, files, verification,
    compatibility. Returning a view here would force every caller to
    re-resolve, defeating the point of having a ``load`` method at
    all.
    """
    service = _service(
        [("bundled:x", _manifest("python.fastapi.api", "FastAPI"), BlueprintSource.OFFICIAL)]
    )

    blueprint = service.load("python.fastapi.api")

    assert isinstance(blueprint, Blueprint)
    assert blueprint.id == "python.fastapi.api"
    assert blueprint.name == "FastAPI"
    # The full document has attributes the view does not; asserting one
    # of them catches a refactor that accidentally returns a view.
    assert hasattr(blueprint, "verification")


def test_load_returns_local_copy_when_duplicated() -> None:
    """``load()`` honours the same precedence as ``discover()``.

    The service re-resolves on every call (ADR-0009's cache policy),
    so ``load`` and ``discover`` must see the same winner. A test
    that only covered ``discover`` would miss a refactor that made
    ``load`` short-circuit to the first candidate.
    """
    service = _service(
        [
            ("bundled:x", _manifest("python.fastapi.api", "Official"), BlueprintSource.OFFICIAL),
            ("local:x", _manifest("python.fastapi.api", "Local"), BlueprintSource.LOCAL),
        ]
    )

    blueprint = service.load("python.fastapi.api")

    assert blueprint.name == "Local"


# -----------------------------------------------------------------------------
# load() — missing id
# -----------------------------------------------------------------------------
def test_load_raises_discovery_error_for_unknown_id() -> None:
    """A missing id raises ``DiscoveryError``, not a bare ``KeyError``.

    The CLI handler dispatches on ``ForgeError`` and renders a
    user-facing message; a ``KeyError`` would escape to the generic
    internal-error branch and produce a stack trace. The test asserts
    the domain error type, not just "some exception", so a refactor
    that replaces ``DiscoveryError`` with a dict lookup is caught.
    """
    service = _service([("bundled:x", _manifest("python.fastapi.api"), BlueprintSource.OFFICIAL)])

    with pytest.raises(DiscoveryError) as info:
        service.load("does.not.exist")

    assert "does.not.exist" in str(info.value)
    # The hint ("run `forge list`") is what turns the failure into a
    # recovery. The message asserts the id; the hint asserts the
    # recovery path is named.
    assert "forge list" in str(info.value)


def test_load_unknown_id_lists_available_ids() -> None:
    """The error message names the ids that *are* available.

    A bare "not found" forces the user to run a second command. Naming
    the alternatives in the same message is a one-keystroke fix; this
    test locks the behaviour in so a future message tightening does
    not lose it.
    """
    service = _service(
        [
            ("bundled:a", _manifest("alpha.one"), BlueprintSource.OFFICIAL),
            ("bundled:b", _manifest("beta.one"), BlueprintSource.OFFICIAL),
        ]
    )

    with pytest.raises(DiscoveryError) as info:
        service.load("nope")

    message = str(info.value)
    # Both available ids appear, so a user typing a close-but-wrong id
    # can see the correct spelling without running another command.
    assert "alpha.one" in message
    assert "beta.one" in message


# -----------------------------------------------------------------------------
# Source injection
# -----------------------------------------------------------------------------
def test_injected_sources_fully_replace_defaults() -> None:
    """When ``sources`` is provided, ``default_sources`` is not consulted.

    This is the seam a future ``--blueprint-dir`` flag will use, so it
    is load-bearing: if the service *added* injected sources to the
    defaults rather than replacing them, a user-specified directory
    would be unable to *hide* an official blueprint (they would always
    see both). Asserting "the bundled source is not present" locks
    the semantics in.

    The test uses a real file on disk because the alternative — checking
    that ``_resolved_sources`` equals the injected list — would couple
    the test to a private method. Reading the catalogue is the public
    contract; asserting on it via the public contract is what keeps the
    test honest.
    """
    service = _service([("fake:x", _manifest("only.one"), BlueprintSource.LOCAL)])

    result = service.discover()

    # The only blueprint in the catalogue is the one the fake source
    # provided. If ``default_sources`` were consulted, the bundled
    # catalogue would appear here too, and this assertion would fail
    # for every blueprint the install ships.
    assert [bp.id for bp in result] == ["only.one"]


def test_none_sources_uses_defaults() -> None:
    """``sources=None`` means "use the standard composition".

    The service's constructor default is ``None``, and the fixture
    relies on it. Asserting the behaviour explicitly catches a
    refactor that changes the default to ``[]`` (which would
    silently disable all discovery) or to a hardcoded list (which
    would break the ``--blueprint-dir`` extension point).
    """
    service = BlueprintDiscoveryService(sources=None)

    # We cannot assert on the *content* of the default catalogue
    # without depending on the bundled blueprints, which changes
    # independently of the service. What we *can* assert is that
    # ``_resolved_sources`` is non-empty and contains a bundled
    # source — the property the service relies on to be useful at all.
    resolved = service._resolved_sources()
    assert resolved, "default composition must include at least one source"
    assert any(
        type(src).__name__ == "BundledSource" for src in resolved
    ), "default composition must include the bundled source"


# -----------------------------------------------------------------------------
# DEFAULT_LOCAL_ROOT
# -----------------------------------------------------------------------------
def test_default_local_root_is_cwd_relative() -> None:
    """The default local root is ``.forge/blueprints`` under the CWD.

    This is a design decision documented in ``service.py``: blueprints
    are project-shaped artefacts, so per-project overrides are the
    natural unit of customisation. Asserting the relationship (rather
    than the literal path) catches a refactor that switches to an
    absolute home-directory path — which would be a silent, breaking
    behaviour change.
    """
    # ``DEFAULT_LOCAL_ROOT`` is computed at import time using the CWD
    # of the importing process, so we cannot compare against
    # ``Path.cwd()`` without knowing when ``service`` was imported.
    # What we *can* assert is the trailing structure, which is the
    # part that matters to users.
    assert DEFAULT_LOCAL_ROOT.name == "blueprints"
    assert DEFAULT_LOCAL_ROOT.parent.name == ".forge"
    assert DEFAULT_LOCAL_ROOT.is_absolute()


# -----------------------------------------------------------------------------
# Integration with real sources (filesystem-backed)
# -----------------------------------------------------------------------------
def test_service_with_real_local_source_and_no_bundled(tmp_path: Path) -> None:
    """End-to-end sanity check with a real ``LocalDirectorySource``.

    Every other test in this file uses a fake source to stay isolated
    from filesystem layout. This one deliberately uses the real
    source so that a change to ``LocalDirectorySource.candidates()``
    that the source-level tests miss (e.g., a layout change) fails
    here too — through the service, which is where the CLI actually
    consumes it.

    The bundled source is excluded by injecting an explicit source
    list, so the test does not depend on which official blueprints
    happen to ship.
    """
    from forge.discovery.sources import LocalDirectorySource

    bp_dir = tmp_path / "my.custom.bp"
    bp_dir.mkdir()
    (bp_dir / "blueprint.yaml").write_text(
        textwrap.dedent(
            """\
            schema_version: "1.0.0"
            id: my.custom.bp
            name: My Custom Blueprint
            version: 2.3.4
            description: a local blueprint from a real directory
            category: automation
            verification:
              - id: ok
                type: required_file
                path: README.md
            compatibility:
              min_forge_version: 1.0.0
            """
        ),
        encoding="utf-8",
    )

    service = BlueprintDiscoveryService(sources=[LocalDirectorySource(root=tmp_path)])

    catalogue = service.discover()

    assert len(catalogue) == 1
    assert catalogue[0].id == "my.custom.bp"
    assert catalogue[0].version == "2.3.4"
    assert catalogue[0].source is BlueprintSource.LOCAL
