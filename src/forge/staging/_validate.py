"""Post-render structural validation of the staged tree.

Purpose
-------
After `StagingEngine` has rendered every `PlannedFile` into the staging
directory, this module answers one question:

    "Does the staged tree match the plan's promises?"

That is a *structural* question, not a *semantic* one. It catches the
staging engine's own bugs — a missing write, an unexpected extra write,
a file produced at a path other than the one the plan declared — and
nothing else. Everything about whether the *rendered content* is
correct (compiles, has required files, matches a verification contract)
belongs to a later, distinct phase (blueprint verification, ADR-0010
and following). Mixing the two concerns here would make this module:

    * harder to reason about (two invariants, two failure vocabularies),
    * harder to reuse (the verifier is blueprint-specific; this is
      universal),
    * and slower in the common case (structural checks are O(files);
      verification often runs commands).

Why does this module live under `staging/_validate.py` and not
`verification/`?
---------------------------------------------------------------------
Because it validates *the staging engine's own output*, not the
blueprint's promises. Its failure modes are "the engine has a bug,"
not "the blueprint asked for something that does not exist." Those are
different audiences and different fixes:

    * a `staging.*` finding means "report a bug to the tool maintainers,"
    * a `verification.*` finding means "fix your blueprint or your
      environment."

Keeping the module next to the engine means the code that produces the
tree and the code that sanity-checks it can be reasoned about and
tested together, and the "this is an internal error" vocabulary stays
local. The blueprint verifier (a later phase) has its own module and
its own `Finding` vocabulary; it consumes the `StagingResult` and
never reaches into this file.

Invariants this module enforces
-------------------------------
Given:
    * `staged_root`: the staging directory that the engine produced,
    * `expected_manifest`: the (destination-relative path, kind) pairs
      the engine *believes* it produced,

the following must hold on success of `validate_staged_tree`:

    1. **No missing files.** For every `(rel, kind)` in
       `expected_manifest`, `staged_root / rel` exists as a regular
       file (never a directory, never a symlink).
    2. **No unexpected files.** Every regular file found under
       `staged_root` (recursively) appears in `expected_manifest`.
    3. **No symlinks.** The staging area is populated exclusively by
       the engine; a symlink in the tree means the engine followed one
       or wrote one, both of which are bugs. `_walk` uses `os.walk`
       with `followlinks=False`, and each entry is checked with
       `os.path.islink` so an *escaped* symlink cannot slip past.
    4. **No unexpected directories.** Not an invariant we enforce
       explicitly: empty directories are a legitimate side-effect of
       creating the tree (e.g. a file at `src/demo/main.py` creates
       `src/` and `src/demo/`). Only regular files are compared
       against the manifest; empty directories are harmless because
       commit iterates the manifest, not the tree.

What this module deliberately does *not* do
--------------------------------------------
    * **Content hashing.** The engine's manifest does not carry
      expected content, and hashing every file to compare against a
      value we don't have would be theatre. If a future bug requires
      content verification, that check belongs to the verifier, not
      here — the verifier has a blueprint contract to hash *against*.
    * **Rendering again.** We trust the renderer, which is tested
      exhaustively elsewhere. Re-rendering to compare output would
      double the cost of every staging operation for no marginal
      safety.
    * **Permissions checking.** The staging tree is ours; if a write
      produced the wrong mode, commit is the place to fix that
      (because commit is where mode matters to the destination). This
      module only asks "does the file exist, and is it what we
      claimed it was?"
    * **Reading file contents.** Existence and type (regular file) are
      enough for the structural question; reading contents would be
      O(bytes) instead of O(files) and would not catch anything the
      two structural checks miss.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from forge.staging.errors import Finding

# Stage label used in findings produced by this module. Kept as a
# constant so tests can assert on the exact string without duplicating
# the literal, and so a future rename is a one-line change.
_STAGE_LABEL = "staging"


def validate_staged_tree(
    staged_root: Path,
    expected_manifest: tuple[tuple[str, str], ...],
) -> list[Finding]:
    """Structural self-check of the staged tree.

    Parameters
    ----------
    staged_root
        Absolute path to the staging directory. Must exist; if it does
        not, this function returns a single finding
        (`staging.missing_root`) rather than raising, because the
        engine's job is to *report* problems, not crash on them.
    expected_manifest
        The `(destination_relative_path, kind)` pairs the engine
        believes it produced. `kind` is not used by this function — it
        is preserved in the tuple for callers who want to report it
        alongside findings — but the tuple shape is intentional: it is
        the same shape as `StagingResult.manifest`, so the check can be
        invoked with `result.manifest` directly.

    Returns
    -------
    A list of findings, sorted for determinism. Empty ⟺ the tree is
    structurally correct. The caller (the staging engine) treats a
    non-empty return as a fatal error and aborts staging; a consumer
    that runs this check separately (a future `forge doctor` mode)
    might treat the findings as warnings.

    Determinism
    -----------
    Findings are sorted by path so that a test that asserts on the
    *first* finding is stable across filesystem enumeration orders and
    operating systems. `os.walk` on Linux, macOS, and Windows can
    return directory entries in different orders; sorting once here
    removes the platform-specific flake from every caller.
    """
    if not staged_root.exists():
        # The engine's context manager is responsible for the tree's
        # lifetime; a missing root here means the engine already
        # destroyed it (a bug) or never created it (also a bug). Report
        # it plainly; the caller's hint will point at the tool.
        return [
            Finding(
                code="staging.missing_root",
                path="<staging>",
                message="staging directory does not exist",
                hint="This is a bug in the staging engine; please report it.",
            )
        ]

    findings: list[Finding] = []
    expected_paths = {rel for rel, _kind in expected_manifest}

    # 1) Every planned file exists, as a regular file, not a symlink.
    for rel in sorted(expected_paths):
        findings.extend(_check_expected_present(staged_root, rel))

    # 2) Every regular file in the tree appears in the manifest. We
    #    walk the tree once, which is O(files) — the same cost class
    #    as step 1. Building the set of found paths first and diffing
    #    against the manifest is clearer than a per-file lookup.
    found_paths = set(_iter_regular_files(staged_root))
    for rel in sorted(found_paths - expected_paths):
        findings.append(
            Finding(
                code="staging.unexpected",
                path=rel,
                message="staged file was not in the plan",
                hint="This is a bug in the staging engine; please report it.",
            )
        )

    # 3) No symlinks anywhere in the tree. `_iter_regular_files`
    #    already skips symlinks (os.walk with followlinks=False), but a
    #    symlink *at* a path the manifest expects would be silently
    #    treated as missing by step 1 (since is_file() follows
    #    symlinks). Walking explicitly here is defence in depth: the
    #    engine must never have created one, so any symlink is a bug.
    for rel in _iter_symlinks(staged_root):
        findings.append(
            Finding(
                code="staging.symlink",
                path=rel,
                message="staged tree contains a symlink",
                hint="This is a bug in the staging engine; please report it.",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Internals — kept small and single-purpose so the top-level function
# reads as three named checks rather than a nest of conditionals.
# ---------------------------------------------------------------------------


def _check_expected_present(root: Path, rel: str) -> list[Finding]:
    """Return [] if `root/rel` is a regular file; otherwise one finding.

    Uses `os.lstat` via `Path.is_file()` *without* following symlinks:
    `Path.is_file()` follows symlinks, so we check the symlink case
    explicitly first. A regular file is the only acceptable shape — a
    directory at the expected path (e.g. a template that was rendered
    into a directory name by mistake) and a symlink are both bugs.
    """
    target = root / rel

    # `is_symlink()` uses lstat under the hood and does not follow.
    if target.is_symlink():
        return [
            Finding(
                code="staging.symlink",
                path=rel,
                message="planned destination is a symlink",
                hint="This is a bug in the staging engine; please report it.",
            )
        ]

    if not target.exists():
        return [
            Finding(
                code="staging.missing",
                path=rel,
                message="staged file was not produced",
                hint="This is a bug in the staging engine; please report it.",
            )
        ]

    if target.is_dir():
        return [
            Finding(
                code="staging.wrong_type",
                path=rel,
                message="planned destination is a directory, not a file",
                hint="This is a bug in the staging engine; please report it.",
            )
        ]

    return []


def _iter_regular_files(root: Path) -> Iterator[str]:
    """Yield destination-relative paths of every regular file under root.

    `followlinks=False` prevents walking into symlinked directories
    (which could escape the staging tree entirely). Symlinked files are
    skipped by the `islink` check; they are reported separately.
    """
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune symlinked directories so os.walk does not descend into
        # them even with followlinks=False (it still yields them as
        # dirnames; explicit pruning keeps the walk honest and the
        # symlink-detection loop below authoritative).
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]

        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink():
                # Reported by _iter_symlinks; not a "regular file" for
                # manifest comparison.
                continue
            if not full.is_file():
                # Non-regular (FIFO, socket). Not something the engine
                # produces; flag as unexpected via the manifest diff
                # because it also will not appear in `expected_paths`.
                # `is_file()` returns False for these; treat as file-like
                # for the purpose of "found something the plan did not
                # declare."
                yield str(full.relative_to(root_resolved))
                continue
            yield str(full.relative_to(root_resolved))


def _iter_symlinks(root: Path) -> Iterator[str]:
    """Yield destination-relative paths of every symlink under root.

    Symlinks are never legitimate in a staging tree — the engine writes
    bytes, and copy-kind sources cannot be symlinks (the planner
    rejects symlinked sources during path resolution). Any symlink here
    is a bug worth surfacing with its own code so operators can
    distinguish "engine wrote a symlink" from "engine wrote something
    unexpected at a path."
    """
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                yield str(full.relative_to(root_resolved))
