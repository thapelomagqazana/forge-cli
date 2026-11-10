"""The FileMapper — planning phase as a pure, total function.

What this module does
---------------------
Given a *validated* blueprint and a *resolved* variable mapping, it
produces the concrete list of files that generation will write:

    source (relative to blueprint root)
        ├── destination (already substituted, already validated for safety)
        └── kind (render or copy)

What this module deliberately does **not** do
--------------------------------------------
* **No I/O writes.** The plan is data; writers live elsewhere.
* **No rendering of file *bodies*.** Only destination *paths* are
  substituted, through the same sandboxed renderer used for bodies
  (so path substitution cannot become a capability escape hatch).
* **No condition evaluation.** Conditional files are a separate,
  explicit phase; a future evaluator will call `_plan_one` for each
  entry whose condition holds. Keeping the seam visible prevents the
  classic bug where everything renders even when it shouldn't.
* **No filesystem reads.** The loader already verified template
  existence; the planner trusts the validated blueprint.

Contract
--------
    FileMapper().map(blueprint, variables)
        -> Result[list[PlannedFile], MappingError]

Guarantees (each covered by tests)
----------------------------------
1. Pure: no global state, no clock, no environment.
2. Total: every error path returns `Err(MappingError)`; nothing raises.
3. Deterministic: identical inputs ⇒ identical output, sorted by
   destination.
4. Aggregate: all findings are collected in one `MappingError`;
   collisions are reported on the *normalised* destination, listing
   every colliding source — never last-writer-wins.
"""

from __future__ import annotations

from collections.abc import Mapping

from forge.loading.result import Err, Ok, Result
from forge.models import Blueprint, FileEntry
from forge.planning.collisions import find_collisions
from forge.planning.destinations import resolve
from forge.planning.errors import Finding, MappingError
from forge.planning.planned_file import PlannedFile, PlannedFileKind

# A small, explicit allowlist of template extensions. Adding one is a
# one-line, reviewable change; adding dozens is a design discussion.
_TEMPLATE_EXTENSIONS: tuple[str, ...] = (".j2", ".jinja", ".jinja2")

# Binary/opaque extensions that must never be rendered even if their name
# happens to end in a template extension (defense in depth — this can only
# trigger if a blueprint author deliberately named a binary `logo.png.j2`,
# in which case we still copy, never corrupt).
_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".whl",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".webm",
        ".wav",
        ".sqlite",
        ".sqlite3",
        ".db",
    }
)


class FileMapper:
    """Turn a blueprint's `files[]` into a manifest of planned writes.

    Stateless and safe to reuse across calls; keep one instance in
    scaffolding code rather than constructing per file.
    """

    def map(
        self,
        blueprint: Blueprint,
        variables: Mapping[str, object],
    ) -> Result[list[PlannedFile], MappingError]:
        """Plan the writes for `blueprint` given resolved `variables`.

        The returned list is sorted by `destination` and deduplicated
        against collisions before being returned, so downstream writers
        can iterate without further checks.
        """
        findings: list[Finding] = []
        planned: list[PlannedFile] = []

        # Unconditional files first. Conditional files are planned by a
        # dedicated phase (Task 1.3.3) once condition evaluation exists;
        # that phase will reuse `_plan_one` so we do not duplicate path
        # resolution or kind detection.
        for entry in blueprint.files:
            self._plan_one(entry, variables, planned, findings)

        # Collision detection only when structural checks passed —
        # otherwise the report would be noisy with secondary findings.
        if not findings:
            findings.extend(find_collisions(planned))

        if findings:
            return Err(
                MappingError(
                    findings=tuple(findings),
                    source_blueprint=blueprint.id,
                )
            )

        planned.sort(key=lambda p: p.destination)
        return Ok(planned)

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _plan_one(
        self,
        entry: FileEntry,
        variables: Mapping[str, object],
        planned: list[PlannedFile],
        findings: list[Finding],
    ) -> None:
        """Resolve one `files[]` entry into a `PlannedFile` or a finding.

        Splitting this out from `map` keeps the loop body trivially
        readable and lets the conditional phase reuse it without
        copy-pasting the source/destination logic.
        """
        source_finding = self._check_source(entry.source)
        if source_finding is not None:
            findings.append(source_finding)
            return

        destination, dest_finding = resolve(entry.destination, variables)
        if dest_finding is not None:
            findings.append(dest_finding)
            return

        planned.append(
            PlannedFile(
                source=entry.source,
                destination=destination,
                kind=self._kind_for(entry.source, destination),
            )
        )

    def _check_source(self, source: str) -> Finding | None:
        """Validate a source path before it reaches the writer.

        Sources are resolved against the blueprint directory, so the
        rules are the same class as destination rules: relative only,
        no traversal, no absolute paths. Centralising the checks here
        means `conditional_files[]` inherits them for free.
        """
        if not source:
            return Finding(
                destination="",
                message="source path is empty",
                hint="Give the files[] entry a source.",
                code="source.empty",
            )
        if "\x00" in source or "\n" in source:
            return Finding(
                destination=source,
                message="source contains a NUL byte or newline",
                hint="Remove the offending character from the source path.",
                code="source.illegal_chars",
                sources=(source,),
            )
        if source.startswith(("/", "\\")) or (len(source) > 1 and source[1] == ":"):
            return Finding(
                destination=source,
                message="source must be relative, not absolute",
                hint="Sources are resolved against the blueprint directory.",
                code="source.absolute",
                sources=(source,),
            )
        parts = source.replace("\\", "/").split("/")
        if any(part == "" for part in parts):
            return Finding(
                destination=source,
                message="source contains an empty path segment",
                hint="Check for doubled slashes.",
                code="source.empty_segment",
                sources=(source,),
            )
        if any(part in {"..", "."} for part in parts):
            return Finding(
                destination=source,
                message="source may not contain '..' or '.' segments",
                hint="Sources are resolved against the blueprint directory.",
                code="source.traversal",
                sources=(source,),
            )
        return None

    def _kind_for(self, source: str, destination: str) -> PlannedFileKind:
        """Decide whether a file is rendered as a template or copied.

        Rules (documented in PLANNING.md, exercised by tests):

        1. Binary extension on *either* side ⇒ copy. (Never render a
           binary; that corrupts it.)
        2. Source does not end in a template extension ⇒ copy. (A
           non-template source named without an extension is assumed
           literal — rendering it would be a surprise.)
        3. Otherwise ⇒ render.

        The conservative bias ("when in doubt, copy") matters: silently
        copying a template would emit `{{ … }}` into the output, which
        is ugly but safe; silently rendering a binary would corrupt it,
        which is neither.
        """
        if _has_binary_extension(source) or _has_binary_extension(destination):
            return PlannedFileKind.COPY
        if not source.endswith(_TEMPLATE_EXTENSIONS):
            return PlannedFileKind.COPY
        return PlannedFileKind.RENDER


def _has_binary_extension(path: str) -> bool:
    """Return True when `path` ends in a known binary/opaque extension.

    Case-insensitive so `Logo.PNG` and `logo.png` are handled the same.
    Uses `str.endswith(tuple)` so no regex or path-parsing is needed —
    the operation is O(len) and allocation-free in the common case.
    """
    return path.lower().endswith(tuple(_BINARY_EXTENSIONS))
