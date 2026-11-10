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
* **No condition evaluation.** Conditional files arrive pre-filtered by
  `ConditionalEvaluator` (see `forge.conditions`); this module receives
  the surviving groups and *plans* them. Keeping the seam visible
  prevents the classic bug where everything renders even when it
  shouldn't — the caller must opt into conditional inclusion, it is
  never a silent default.
* **No filesystem reads.** The loader already verified template
  existence; the planner trusts the validated blueprint.

Contract
--------
    FileMapper().map(blueprint, variables)
        -> Result[list[PlannedFile], MappingError]

    FileMapper().map(blueprint, variables, conditional_groups=groups)
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
5. Conditional-by-explicit-request: conditional groups are only planned
   when the caller passes them. The default (no `conditional_groups`
   argument) is unconditional files only. That is a safety default, not
   an oversight: silently including optional components because the
   caller forgot to filter them is exactly the class of bug a planner
   exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


# ---------------------------------------------------------------------------
# Conditional group shape
#
# The evaluator produces tuples of (parsed_expression, files). We accept a
# structural type rather than importing the evaluator's `SurvivingGroup`
# alias, so:
#   * the planner does not depend on `forge.conditions` (one-way graph),
#   * any caller that already has "groups of files that survived" can
#     pass them without importing conditions,
#   * future condition sources (e.g. a `manifest.json` variant) can plug
#     in without touching this module.
# ---------------------------------------------------------------------------
ConditionalGroups = "list[tuple[Any, list[FileEntry]]]"
"""Shape: list of (parsed_condition_ast, files). The AST is opaque to us —
we plan the files; the evaluator owned the decision to include them."""


class FileMapper:
    """Turn a blueprint's files into a manifest of planned writes.

    Stateless and safe to reuse across calls; keep one instance in
    scaffolding code rather than constructing per file.
    """

    def map(
        self,
        blueprint: Blueprint,
        variables: Mapping[str, object],
        *,
        conditional_groups: list[tuple[Any, list[FileEntry]]] | None = None,
    ) -> Result[list[PlannedFile], MappingError]:
        """Plan the writes for `blueprint` given resolved `variables`.

        Parameters
        ----------
        blueprint
            A validated `Blueprint`. Only `files`, `conditional_files`,
            and `id` are read; nothing is mutated.
        variables
            Resolved variable values, already validated by
            `VariableResolver`. Used for destination substitution only.
        conditional_groups
            Groups that survived `ConditionalEvaluator`. Each entry is
            `(parsed_ast, files)` — the AST is opaque to the planner.
            When `None` (default), conditional files are **not**
            included; callers that want them must pass the evaluator's
            output. This is the "safe by default" rule documented in
            ADR-0015.

        Returns
        -------
        Ok(list[PlannedFile])
            Every file (unconditional + surviving conditional),
            sorted by destination, collision-free.
        Err(MappingError)
            Aggregated findings: source/destination safety problems,
            empty substitutions, and destination collisions. Every
            finding is reported — never just the first.
        """
        findings: list[Finding] = []
        planned: list[PlannedFile] = []

        # Unconditional files first — they are the blueprint's core.
        # We iterate the tuple/list as declared so that, absent
        # collisions, downstream ordering is stable across runs.
        for entry in blueprint.files:
            self._plan_one(entry, variables, planned, findings)

        # Conditional files next. `conditional_groups or ()` treats both
        # None (caller did not opt in) and an empty list (evaluator found
        # nothing to include) identically: no conditional files planned.
        # Distinguishing them would add no behaviour; the caller's
        # intent is visible at the call site.
        for _parsed_condition, files in conditional_groups or ():
            for entry in files:
                self._plan_one(entry, variables, planned, findings)

        # Collision detection only when structural checks passed —
        # otherwise the report would be noisy with secondary findings
        # ("duplicate x" when x itself is an invalid path).
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
        """Resolve one file entry into a `PlannedFile` or a finding.

        Splitting this out from `map` keeps the loop body trivially
        readable and lets the conditional phase reuse it without
        copy-pasting the source/destination logic. It also means a new
        file source (e.g. a future `assets[]` list) can be planned with
        one loop, not a rewrite.
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
        means `conditional_files[]` inherits them for free — a
        conditional entry is held to exactly the same safety standard
        as an unconditional one.
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
        copying a template would emit ``{{ … }}`` into the output, which
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
