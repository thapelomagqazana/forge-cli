"""Unit tests for ``forge.discovery.sources``.

Scope
-----
The sources layer is the *only* place in discovery that touches the
filesystem. Every other module consumes ``candidates()`` streams and
never reads bytes directly. That makes this file the sole boundary at
which I/O failures must be handled, and the sole place where the
contract "a source yields candidates and nothing else" is enforced.

What is tested here
-------------------
* ``BundledSource`` and ``LocalDirectorySource`` produce candidates in
  deterministic order, with the correct source label and origin.
* Missing directories, missing packages, unreadable files, and
  unreadable directories all degrade to "no candidates" (or an
  empty-bytes candidate the resolver can warn about), never to an
  exception that reaches the user.
* ``MANIFEST_FILENAME`` is a module-level constant shared by both
  sources, so a future layout change is a one-line edit.
* ``default_sources`` composes the two standard layers in the
  documented order.

What is *not* tested here
-------------------------
* Precedence between sources, deduplication, warnings — those are the
  resolver's job and are tested in ``test_resolver.py``.
* The service-level composition, sorting, and error translation —
  tested in ``test_service.py``.
* Real bundled blueprint content — tested in
  ``tests/integration/test_discovery_bundled.py``, which runs against
  a built wheel.

The tests use monkeypatched I/O (``Path.read_bytes``,
``Path.iterdir``) rather than real filesystem permissions because
permission-based tests behave differently under root, in containers,
and on Windows. A test that passes on one machine and fails on
another is worse than no test at all.

Design invariants this file pins
--------------------------------
1. **Sources never raise.** Every public method either yields
   candidates or yields nothing; no ``OSError``, ``ImportError``, or
   ``AttributeError`` escapes. This is what lets the resolver assume
   "one bad source does not take down the catalogue."
2. **Sorting is source-level.** Each source yields candidates in a
   deterministic order. The service relies on this for stable warning
   output; testing it here means a service-level refactor cannot
   accidentally break warning stability by reordering.
3. **Empty-bytes candidates are the single malformed signal.** A
   source that cannot read a file yields ``(label, b"", origin)``
   rather than skipping the candidate or raising. The resolver has
   exactly one branch for "malformed" because of this invariant.
4. **Labels are prefix-tagged.** Bundled candidates are labelled
   ``"bundled:<name>"``; local candidates are labelled
   ``"local:<absolute path>"``. The resolver's warnings rely on the
   prefix for context, and tests assert on the prefix so a future
   label format change is caught here.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from forge.discovery.sources import (
    MANIFEST_FILENAME,
    BundledSource,
    Candidate,
    LocalDirectorySource,
    default_sources,
)
from forge.models.blueprint import BlueprintSource


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _write_manifest(root: Path, blueprint_id: str, body: bytes = b"x") -> Path:
    """Create a blueprint directory under ``root`` and return its manifest path.

    The manifest body is deliberately uninteresting — sources do not
    parse, so the content is irrelevant to what these tests assert.
    Tests that care about the *content* belong in the schema or model
    test suites; tests that care about the *stream* belong here.
    """
    manifest = root / blueprint_id / MANIFEST_FILENAME
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(body)
    return manifest


def _by_label(candidates: list[Candidate]) -> dict[str, tuple[bytes, BlueprintSource]]:
    """Index candidates by label for order-independent assertions.

    A test that iterates a sorted list of labels is asserting on
    *order*; a test that iterates a dict is asserting on *content*.
    Keeping the two separate makes the intent of each test explicit —
    ``test_yields_in_sorted_order`` should fail if the sort is broken;
    ``test_yields_all_manifests`` should not.
    """
    return {label: (raw, origin) for label, raw, origin in candidates}


# -----------------------------------------------------------------------------
# MANIFEST_FILENAME — the shared layout rule
# -----------------------------------------------------------------------------
class TestManifestFilename:
    """The filename is a shared constant, not a literal in two places."""

    @pytest.mark.unit()
    def test_is_the_documented_filename(self) -> None:
        """Both sources use the same manifest filename.

        Asserting the literal value here locks the layout rule in: a
        change to ``blueprint.yaml`` would break every blueprint on
        disk, so it must be a deliberate, reviewable diff — not a
        silent constant flip.
        """
        assert MANIFEST_FILENAME == "blueprint.yaml"

    @pytest.mark.unit()
    def test_both_source_classes_use_the_shared_constant(self, tmp_path: Path) -> None:
        """Neither source inlines the filename as a string literal.

        The two classes default ``filename`` to ``MANIFEST_FILENAME``
        rather than to a literal. If a future contributor adds a third
        source, this test's spirit is that the constant should be reused;
        if they instead hard-code ``"blueprint.yaml"``, the layout rule
        has two definitions and will drift.

        ``LocalDirectorySource`` requires a ``root`` at construction time.
        The test uses ``tmp_path`` rather than any fixed directory because:

        * ``root`` is never read on the *read* path — the assertion is on
        the ``filename`` field, not on ``root`` — so the value is
        irrelevant to what the test proves. A real fixture directory
        says exactly that.
        * A hard-coded path like ``/tmp`` (which Bandit flags with
        ``S108``) reads as "this test touches the filesystem," which it
        does not. ``tmp_path`` is the pytest-idiomatic way to say "a
        path that exists and is disposable, in case the value ever
        matters" without implying anything about how it is used.
        """
        assert BundledSource().filename == MANIFEST_FILENAME
        assert LocalDirectorySource(root=tmp_path).filename == MANIFEST_FILENAME


# -----------------------------------------------------------------------------
# BundledSource — happy path
# -----------------------------------------------------------------------------
class TestBundledSourceHappyPath:
    """``BundledSource.candidates()`` on a well-formed package."""

    @pytest.mark.unit()
    def test_yields_every_manifest_in_the_package(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Every directory with a ``blueprint.yaml`` yields one candidate.

        Uses a fake package directory instead of the real one so the
        test does not depend on which official blueprints ship. The
        real bundled content is tested in
        ``tests/integration/test_discovery_bundled.py`` against a
        built wheel.
        """
        _write_manifest(tmp_path, "python.fastapi.api", b"fastapi-manifest")
        _write_manifest(tmp_path, "python.automation.cli", b"cli-manifest")
        _patch_bundled_root(monkeypatch, tmp_path)

        candidates = list(BundledSource().candidates())

        assert len(candidates) == 2
        by_label = _by_label(candidates)
        assert by_label["bundled:python.automation.cli"] == (
            b"cli-manifest",
            BlueprintSource.OFFICIAL,
        )
        assert by_label["bundled:python.fastapi.api"] == (
            b"fastapi-manifest",
            BlueprintSource.OFFICIAL,
        )

    @pytest.mark.unit()
    def test_yields_in_sorted_order(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Candidates are yielded in sorted-by-directory order.

        The resolver's warning output depends on this ordering: two
        runs against the same package must produce identical warning
        sequences, or CI logs become un-diffable and snapshot tests
        become flaky. If this assertion fails, someone replaced
        ``sorted(...)`` with a raw ``iterdir()``.
        """
        _write_manifest(tmp_path, "z.last")
        _write_manifest(tmp_path, "a.first")
        _write_manifest(tmp_path, "m.middle")
        _patch_bundled_root(monkeypatch, tmp_path)

        labels = [label for label, _, _ in BundledSource().candidates()]

        assert labels == [
            "bundled:a.first",
            "bundled:m.middle",
            "bundled:z.last",
        ]

    @pytest.mark.unit()
    def test_skips_directories_without_a_manifest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A directory without ``blueprint.yaml`` is not a blueprint.

        Common case: a ``templates/`` directory or a future
        non-blueprint resource that happens to live under the package.
        Silently skipping (not warning) is correct — the resolver's
        warnings should be actionable, not noise about non-entities.
        """
        (tmp_path / "not-a-blueprint").mkdir()
        _write_manifest(tmp_path, "real.one")
        _patch_bundled_root(monkeypatch, tmp_path)

        candidates = list(BundledSource().candidates())

        assert [label for label, _, _ in candidates] == ["bundled:real.one"]

    @pytest.mark.unit()
    def test_skips_files_at_the_package_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A plain file in the package is not a candidate.

        An ``__init__.py`` or a stray ``README.md`` at the package root
        must not be treated as a blueprint directory. Without the
        ``is_dir()`` check, ``candidates()`` would try to read
        ``<file>/blueprint.yaml`` and fail with ``NotADirectoryError``.
        """
        (tmp_path / "__init__.py").write_text("# package marker")
        _write_manifest(tmp_path, "real.one")
        _patch_bundled_root(monkeypatch, tmp_path)

        candidates = list(BundledSource().candidates())

        assert [label for label, _, _ in candidates] == ["bundled:real.one"]


# -----------------------------------------------------------------------------
# BundledSource — failure paths
# -----------------------------------------------------------------------------
class TestBundledSourceFailurePaths:
    """``BundledSource`` degrades gracefully on I/O and packaging errors."""

    @pytest.mark.unit()
    def test_yields_nothing_when_package_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing package is a packaging bug, not a crash.

        ``resources.files("forge.blueprints")`` raises
        ``ModuleNotFoundError`` when the package is not installed —
        for example in a checkout where the source layout changed
        before the package marker was added. Yielding nothing lets
        discovery degrade to "no official blueprints" so the CLI still
        works with local ones, and the integration test catches the
        packaging bug separately.

        The test does not distinguish ``ModuleNotFoundError`` from
        ``TypeError`` because both are caught by the same ``except``
        branch — but the test asserts the *behaviour* (yield nothing),
        not the *implementation* (specific except clause).
        """
        monkeypatch.setattr(
            resources,
            "files",
            _raise_factory(ModuleNotFoundError("no such package")),
        )

        assert list(BundledSource().candidates()) == []

    @pytest.mark.unit()
    def test_yields_nothing_when_files_raises_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TypeError`` from ``resources.files`` is also tolerated.

        Some non-package inputs (a module without ``__path__``, or a
        namespace-package edge case on older Pythons) raise
        ``TypeError`` rather than ``ModuleNotFoundError``. The source
        catches both, because both mean the same thing: "we cannot
        enumerate this package." A test that only covered
        ``ModuleNotFoundError`` would leave a gap for the platform-
        specific case.
        """
        monkeypatch.setattr(
            resources,
            "files",
            _raise_factory(TypeError("'module' object has no __path__")),
        )

        assert list(BundledSource().candidates()) == []

    @pytest.mark.unit()
    def test_yields_empty_bytes_when_a_manifest_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unreadable manifest yields an empty candidate, not a crash.

        The empty candidate carries the same label and origin as a
        readable one, so the resolver treats it identically: "malformed,
        skipped, warned." The alternative — raising or skipping
        silently — would either crash discovery on a permissions issue
        or hide the problem from the user. The warning is the correct
        outcome: the file exists, it cannot be read, the user needs to
        know.

        The test patches ``Path.read_bytes`` on the *class*, not on
        the specific instance, because the source iterates through
        ``resources.files(...)`` results whose concrete type depends on
        the loader. Patching at the class level catches the call
        regardless of which ``Traversable`` implementation the loader
        returns.
        """
        good = _write_manifest(tmp_path, "good.one", b"readable")
        _write_manifest(tmp_path, "bad.two", b"unreadable")
        _patch_bundled_root(monkeypatch, tmp_path)
        _patch_read_bytes(
            monkeypatch,
            # Raise for anything ending in "bad.two/blueprint.yaml";
            # delegate to the original for everything else. This is
            # the only way to test "one unreadable file among several"
            # without depending on real permission bits.
            fail_suffix=Path("bad.two", MANIFEST_FILENAME),
        )

        candidates = list(BundledSource().candidates())
        by_label = _by_label(candidates)

        assert by_label["bundled:good.one"] == (good.read_bytes(), BlueprintSource.OFFICIAL)
        assert by_label["bundled:bad.two"] == (b"", BlueprintSource.OFFICIAL)


# -----------------------------------------------------------------------------
# LocalDirectorySource — happy path
# -----------------------------------------------------------------------------
class TestLocalDirectorySourceHappyPath:
    """``LocalDirectorySource.candidates()`` on a well-formed directory."""

    @pytest.mark.unit()
    def test_yields_every_manifest_in_the_directory(self, tmp_path: Path) -> None:
        """Every directory with a ``blueprint.yaml`` yields one candidate."""
        _write_manifest(tmp_path, "custom.one", b"one")
        _write_manifest(tmp_path, "custom.two", b"two")

        candidates = list(LocalDirectorySource(root=tmp_path).candidates())
        by_label = _by_label(candidates)

        assert len(candidates) == 2
        assert by_label[f"local:{tmp_path / 'custom.one' / MANIFEST_FILENAME}"] == (
            b"one",
            BlueprintSource.LOCAL,
        )
        assert by_label[f"local:{tmp_path / 'custom.two' / MANIFEST_FILENAME}"] == (
            b"two",
            BlueprintSource.LOCAL,
        )

    @pytest.mark.unit()
    def test_yields_in_sorted_order(self, tmp_path: Path) -> None:
        """Candidates are sorted by directory name for warning stability."""
        _write_manifest(tmp_path, "z.last")
        _write_manifest(tmp_path, "a.first")
        _write_manifest(tmp_path, "m.middle")

        labels = [label for label, _, _ in LocalDirectorySource(root=tmp_path).candidates()]

        assert labels == [
            f"local:{tmp_path / 'a.first' / MANIFEST_FILENAME}",
            f"local:{tmp_path / 'm.middle' / MANIFEST_FILENAME}",
            f"local:{tmp_path / 'z.last' / MANIFEST_FILENAME}",
        ]

    @pytest.mark.unit()
    def test_origin_is_local(self, tmp_path: Path) -> None:
        """Every local candidate is tagged ``BlueprintSource.LOCAL``.

        The resolver's precedence table keys on this value; a candidate
        tagged ``OFFICIAL`` from a local directory would silently lose
        to a bundled copy of the same id, which is the opposite of what
        the user intended.
        """
        _write_manifest(tmp_path, "custom.one")

        for _, _, origin in LocalDirectorySource(root=tmp_path).candidates():
            assert origin is BlueprintSource.LOCAL

    @pytest.mark.unit()
    def test_label_includes_the_absolute_manifest_path(self, tmp_path: Path) -> None:
        """The label names the full path, so warnings are actionable.

        A user with a malformed local blueprint needs to know *which*
        file was skipped. A label like ``"local:custom.one"`` would
        force them to guess the layout; the full path lets them ``cat``
        the file directly from the warning.
        """
        manifest = _write_manifest(tmp_path, "custom.one")

        [(label, _, _)] = list(LocalDirectorySource(root=tmp_path).candidates())

        assert label == f"local:{manifest}"


# -----------------------------------------------------------------------------
# LocalDirectorySource — failure paths
# -----------------------------------------------------------------------------
class TestLocalDirectorySourceFailurePaths:
    """``LocalDirectorySource`` degrades gracefully on I/O errors."""

    @pytest.mark.unit()
    def test_absent_directory_yields_nothing(self, tmp_path: Path) -> None:
        """No local directory is the common case, not an error.

        Most users have no ``.forge/blueprints/``. Making that an
        error would force every first-run user to create the directory
        before ``forge list`` works, which is hostile. The resolver
        handles empty streams; the source's job is to produce one.
        """
        src = LocalDirectorySource(root=tmp_path / "does-not-exist")

        assert list(src.candidates()) == []

    @pytest.mark.unit()
    def test_exists_returns_false_for_missing_directory(self, tmp_path: Path) -> None:
        """``exists()`` is the cheap check callers can use before iterating."""
        src = LocalDirectorySource(root=tmp_path / "does-not-exist")

        assert src.exists() is False

    @pytest.mark.unit()
    def test_exists_returns_true_for_present_directory(self, tmp_path: Path) -> None:
        """``exists()`` returns ``True`` when the directory is present."""
        src = LocalDirectorySource(root=tmp_path)

        assert src.exists() is True

    @pytest.mark.unit()
    def test_exists_swallows_oserror(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An unreadable parent directory is treated as "not present".

        Pathological filesystems (network mounts, permission-denied
        parents) raise ``OSError`` from ``is_dir()``. Treating that as
        "not present" means the user still gets the official
        catalogue; a crash here would take down the whole CLI over a
        filesystem oddity in a directory the user may not even care
        about.
        """
        src = LocalDirectorySource(root=tmp_path)
        monkeypatch.setattr(Path, "is_dir", _raise_factory(OSError("EACCES")))

        assert src.exists() is False

    @pytest.mark.unit()
    def test_iterdir_oserror_yields_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An ``OSError`` from ``iterdir`` yields nothing, not a crash.

        The race condition this simulates is real: the directory can
        vanish between ``exists()`` and ``iterdir()`` (a user running
        ``rm -rf .forge`` while ``forge list`` runs). Yielding nothing
        means the official catalogue still loads, which is the
        correct behaviour — the local layer is optional, and losing it
        should not block discovery.
        """
        src = LocalDirectorySource(root=tmp_path)
        monkeypatch.setattr(Path, "iterdir", _raise_factory(OSError("ENOENT")))

        assert list(src.candidates()) == []

    @pytest.mark.unit()
    def test_unreadable_manifest_yields_empty_candidate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unreadable local manifest yields an empty candidate.

        Same rationale as the bundled case: the empty candidate is the
        resolver's single malformed signal, so warnings for unreadable
        and invalid YAML share one format. A permissions issue and a
        syntax error should look the same to the user — "we could not
        use this file" — with the label pointing at the file to fix.
        """
        _write_manifest(tmp_path, "good.one", b"readable")
        _write_manifest(tmp_path, "bad.two", b"unreadable")
        _patch_read_bytes(
            monkeypatch,
            fail_suffix=Path("bad.two", MANIFEST_FILENAME),
        )

        candidates = list(LocalDirectorySource(root=tmp_path).candidates())
        by_label = _by_label(candidates)

        assert by_label[f"local:{tmp_path / 'good.one' / MANIFEST_FILENAME}"][0] == b"readable"
        assert by_label[f"local:{tmp_path / 'bad.two' / MANIFEST_FILENAME}"][0] == b""

    @pytest.mark.unit()
    def test_directories_without_a_manifest_are_skipped(self, tmp_path: Path) -> None:
        """A subdirectory without ``blueprint.yaml`` is not a blueprint."""
        (tmp_path / "not-a-blueprint").mkdir()
        _write_manifest(tmp_path, "real.one")

        candidates = list(LocalDirectorySource(root=tmp_path).candidates())

        assert len(candidates) == 1
        assert candidates[0][0].startswith("local:")


# -----------------------------------------------------------------------------
# default_sources — composition
# -----------------------------------------------------------------------------
class TestDefaultSources:
    """``default_sources`` composes the two layers in the documented order."""

    @pytest.mark.unit()
    def test_returns_bundled_then_local(self, tmp_path: Path) -> None:
        """Bundled comes first; local comes second.

        The order does *not* encode precedence (precedence lives in
        the resolver — see ADR-0009), but it does document which
        layers exist and makes the extension point obvious: a future
        third layer (say, an XDG cache) would append to this list.
        """
        sources = default_sources(tmp_path)

        assert len(sources) == 2
        assert isinstance(sources[0], BundledSource)
        assert isinstance(sources[1], LocalDirectorySource)
        assert sources[1].root == tmp_path

    @pytest.mark.unit()
    def test_local_source_is_configured_with_the_given_root(self, tmp_path: Path) -> None:
        """The local source's root is the caller's path, not a default.

        This is the seam a future ``--blueprint-dir`` flag will use: the
        flag threads a path through ``default_sources``, so the source
        sees the user's choice. A source that ignored its constructor
        argument would silently make the flag do nothing.
        """
        sources = default_sources(tmp_path)
        local = sources[1]

        assert isinstance(local, LocalDirectorySource)
        assert local.root == tmp_path


# -----------------------------------------------------------------------------
# Helpers (kept at the bottom so the tests read top-to-bottom)
# -----------------------------------------------------------------------------
def _patch_bundled_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make ``resources.files("forge.blueprints")`` return ``root``.

    ``resources.files`` returns a ``Traversable``, not a ``Path``. For
    a real directory on disk, ``Path`` satisfies the ``Traversable``
    protocol — ``iterdir``, ``joinpath``, ``is_dir``, ``is_file``, and
    ``read_bytes`` all behave the same way. Using the ``Path`` directly
    keeps the test readable; mocking ``Traversable`` would require
    implementing ten methods to test two.
    """
    monkeypatch.setattr(resources, "files", lambda _package: root)


def _patch_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_suffix: Path,
) -> None:
    """Make ``Path.read_bytes`` raise ``OSError`` for paths ending in ``fail_suffix``.

    Patches at the ``Path`` class level rather than on a specific
    instance because ``resources.files`` may return a
    ``Traversable`` subclass (``zipfile.Path``, for a zipped wheel)
    whose ``read_bytes`` is a different implementation. The class-level
    patch is the only way to intercept the call regardless of which
    implementation is in play.
    """
    original = Path.read_bytes

    def _patched(self: Path) -> bytes:
        if self.parts[-2:] == fail_suffix.parts:
            raise OSError(f"simulated read failure for {self}")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", _patched)


def _raise_factory(exc: BaseException) -> _Raiser:
    """Return a callable that raises ``exc`` when invoked with any arguments."""
    return _Raiser(exc)


class _Raiser:
    """Callable that raises a stored exception when invoked.

    Used with ``monkeypatch.setattr`` to substitute module-level
    functions (``resources.files``, ``Path.is_dir``, etc.) whose
    "implementation" for the purposes of a failure test is just "raise
    this exception." Keeping it as a class rather than a closure lets
    the monkeypatch machinery report a stable repr if something goes
    wrong.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        raise self._exc
