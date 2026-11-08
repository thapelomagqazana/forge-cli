"""Pluggable sources of candidate blueprint files.

Design contract
---------------
A source's *only* job is to yield ``(source_label, raw_bytes, origin)``
tuples. It must not parse, validate, decide precedence, or sort. Those
belong to the resolver (``resolver.py``) and the service (``service.py``).

This separation is the whole point of the module. Precedence rules —
"local overrides official, duplicates warn, malformed is skipped" — live
in exactly one place (the resolver), so adding a new source later (a
remote registry, an XDG cache, a project-local override) means writing
one ``candidates()`` method and registering it. It does *not* mean
re-deriving precedence in a new module.

Failure policy
--------------
Sources never raise to callers. Every I/O or read failure is yielded as
an empty-bytes candidate labelled with the path, so the resolver's
"malformed → skip with warning" branch handles it uniformly. This keeps
the error surface singular: users see one warning format regardless of
whether a file was unreadable, mistyped, or schema-invalid.

Path handling
-------------
The bundled source uses ``importlib.resources`` — never ``__file__`` —
so editable installs, wheels, and zipimports all resolve identically.
The local source uses ``pathlib`` and tolerates a missing directory
silently, because "the user has no local blueprints" is the common case,
not an error.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from forge.models.blueprint import BlueprintSource

# A candidate is the *raw* material a source proposes: where it came from
# (for error messages), the bytes to parse, and which layer it belongs to
# (for precedence). Nothing else crosses the source/resolver boundary.
Candidate = tuple[str, bytes, BlueprintSource]

# The manifest filename, shared by every source so the layout rule lives
# in exactly one place. A blueprint is always ``<dir>/blueprint.yaml``.
MANIFEST_FILENAME = "blueprint.yaml"


@dataclass(frozen=True, slots=True)
class BundledSource:
    """Yields official blueprints shipped inside the wheel.

    Layout expectation::

        <package>/<blueprint_id>/blueprint.yaml

    The directory name is *not* used to derive the blueprint id — the
    manifest is authoritative. Directory naming is a packaging convention
    checked by CI, not a runtime dependency.
    """

    package: str = "forge.blueprints"
    filename: str = MANIFEST_FILENAME

    def candidates(self) -> Iterator[Candidate]:
        """Yield every readable bundled manifest in deterministic order.

        Sorted by directory name so two runs against the same wheel
        produce identical candidate streams — a requirement the resolver
        relies on for stable warning order and snapshot-friendly tests.
        """
        try:
            root = resources.files(self.package)
        except (ModuleNotFoundError, TypeError):
            # A missing bundled package is a packaging bug, not a user
            # error. Yielding nothing lets discovery degrade to "no
            # official blueprints" rather than crashing the CLI — the
            # user can still load local ones, and CI catches the bug via
            # the wheel-level integration test.
            return

        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            manifest = entry.joinpath(self.filename)
            if not manifest.is_file():
                # A directory without a manifest is not a blueprint; it is
                # probably ``templates/`` or a future non-blueprint file.
                # Skip silently — nothing to warn about.
                continue
            label = f"bundled:{entry.name}"
            try:
                raw = manifest.read_bytes()
            except OSError:
                # Yield an empty candidate so the resolver emits the
                # standard "malformed, skipped" warning. One unreadable
                # bundled file must not take down the catalogue.
                yield (label, b"", BlueprintSource.OFFICIAL)
                continue
            yield (label, raw, BlueprintSource.OFFICIAL)


@dataclass(frozen=True, slots=True)
class LocalDirectorySource:
    """Yields blueprints from a user-controlled directory.

    Layout mirrors the bundled source so users can copy an official
    blueprint into ``.forge/blueprints/`` and edit it in place without
    renaming anything::

        <root>/<blueprint_id>/blueprint.yaml

    A missing root is the *normal* case (most users have no local
    blueprints) and yields nothing. This is intentional: forcing users
    to ``mkdir .forge/blueprints`` before running ``forge list`` would
    be hostile, and the resolver already handles empty streams.
    """

    root: Path
    filename: str = MANIFEST_FILENAME

    def exists(self) -> bool:
        """Whether the root directory exists.

        Public so the service can short-circuit ``load(id)`` when no
        local layer is present, and so tests can assert intent without
        re-implementing the check.
        """
        try:
            return self.root.is_dir()
        except OSError:
            # Pathological filesystems (network mounts, permission-denied
            # parents) can raise here. Treat as "not present" — the user
            # still gets the official catalogue.
            return False

    def candidates(self) -> Iterator[Candidate]:
        if not self.exists():
            return

        try:
            children = sorted(self.root.iterdir(), key=lambda p: p.name)
        except OSError:
            # Directory vanished between exists() and iterdir(), or is
            # unreadable. Same policy as a missing directory: yield
            # nothing, let the official layer carry the load.
            return

        for child in children:
            if not child.is_dir():
                continue
            manifest = child / self.filename
            if not manifest.is_file():
                continue
            label = f"local:{manifest}"
            try:
                raw = manifest.read_bytes()
            except OSError:
                yield (label, b"", BlueprintSource.LOCAL)
                continue
            yield (label, raw, BlueprintSource.LOCAL)


# Union of all source types the resolver may see. Kept as a type alias
# (not a runtime union) so callers can accept "any source-shaped object"
# in tests without subclassing — duck typing here is a feature, not a
# compromise: sources have one method, and requiring inheritance would
# make the seam heavier than the thing it separates.
SourceLike = BundledSource | LocalDirectorySource


def default_sources(local_root: Path) -> list[SourceLike]:
    """Return the standard source list for a given local root.

    Ordering documents the layers but does *not* encode precedence:
    precedence lives in ``resolver._PRECEDENCE`` (see ADR-0009). Keeping
    that distinction explicit matters — if a future contributor is
    tempted to reorder this list to change which blueprint wins, they
    will find the resolver's precedence table and use it instead. The
    rules stay in one place.
    """
    return [
        BundledSource(),
        LocalDirectorySource(root=local_root),
    ]
