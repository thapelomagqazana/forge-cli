"""Filesystem checks that are part of *loading*, not scaffolding.

Why this module exists
----------------------
A blueprint can be structurally perfect — every required field present,
every type correct — and still be unusable because a template it
references does not exist on disk. Without this module, that failure
would surface during `forge create` as an `OSError` deep inside the
renderer, minutes after validation "succeeded" and with no path
information, no hint, and no way to catch it during a batch `forge
validate` run.

By treating missing templates as *validation errors* rather than runtime
failures, we get three properties that matter operationally:

    1. **Batched reporting** — a blueprint with three missing templates
       reports all three in one `Err([...])`, alongside any schema or
       model errors from other phases.
    2. **Actionable messages** — each error names the exact path in the
       blueprint (`files.0.source`) and the exact relative path that is
       missing, with a hint pointing at the fix.
    3. **Fail-at-load, not fail-at-create** — `forge validate` catches
       the problem in CI, before a user has committed a broken blueprint.

What is checked
---------------
For every file the blueprint declares, four conditions must hold:

    * **Safe** — the reference is a syntactic path that cannot escape
      the blueprint directory. `planning.paths.is_safe_relative` is the
      single definition of this rule (ADR-0015); the loader delegates
      to it rather than re-implementing the checks. This is what "one
      safety net" means in practice: a rule cannot be fixed in one
      place and forgotten in the other.

    * **Contained** — the resolved template path stays inside the
      blueprint's own directory. This is *not* the same check as
      "safe": a lexically-safe path can still resolve outside the root
      if a symlink points out. `Path.resolve()` + `relative_to` is what
      catches that case. The two checks together are defence in depth —
      the lexical check gives a precise message for the common
      `../` mistake, and the resolved check catches symlink
      escapes that the lexical check cannot see.

    * **Exists** — the path names an actual file (not a directory,
      not a dangling symlink).

    * **Readable** — the CLI can open it. A file that exists but is
      mode `000` would fail during rendering; better to catch it here
      with a message that names permissions as the cause.

We do **not** read the file contents. Content problems (bad Jinja,
broken Python) belong to the renderer, which already has the machinery
to report them with template line numbers. Duplicating content checks
here would mean reading every template twice and would blur the boundary
between "this file is what it claims to be" and "this file will parse as
a template" — two very different questions with two different failure
modes.

Symlink handling
----------------
`Path.resolve()` follows symlinks, and we use the resolved path for both
the containment check and the existence check. This is deliberate:

    * A symlink that points *inside* the blueprint directory is fine —
      it is a legitimate way to share a template between blueprints.
    * A symlink that points *outside* the blueprint directory is
      rejected by the containment check, exactly like a `../` path
      would be. Without resolving, a symlink named `safe.j2` pointing
      at `/etc/passwd` would bypass a purely lexical check.
    * A *dangling* symlink resolves to a path that does not exist, and
      is reported as "missing" — which is what the user needs to know.
      The error message does not distinguish "broken symlink" from
      "missing file" because the fix is the same: create the target or
      remove the reference. (If a future caller needs the distinction,
      `Path.is_symlink()` on the *unresolved* path is available and the
      error's `extra` field can carry it.)

Path traversal is defence in depth
----------------------------------
The schema does not constrain `source` to a path shape; it only
constrains it to a non-empty string. That is correct — path shape is a
filesystem concern, not a document-structure concern, and putting a
regex in the schema would fail on legitimate patterns like `../shared/`
in a hypothetical monorepo layout. The check lives here, at the point
where the path is *interpreted*, so the security boundary is where the
filesystem access happens.

Two layers exist for two distinct threats:

    * **Lexical safety** (`is_safe_relative`) rejects `..` segments,
      absolute paths, empty segments, NUL bytes, and control characters
      *before* touching the filesystem. It is cheap, it produces a
      precise message that names the syntactic problem, and it does not
      depend on the resolution behaviour of the host filesystem. This
      is the primary user-facing check.

    * **Resolved containment** (`relative_to`) rejects any path that
      resolves to a location outside the blueprint root, *including*
      the symlink case the lexical check cannot detect. This is the
      primary security check, and it runs only after the path has
      already passed lexical safety — so its error means something
      subtle happened (a symlink, a case-folding quirk), not a
      user typo.

Both are required. Removing either would leave a real gap:

    * Without `is_safe_relative`, a path like `../../etc/passwd` would
      be caught by containment, but the error message would be the
      generic "escapes the blueprint directory" rather than the more
      actionable "may not contain '..' segments" that names the
      syntactic problem. The lexical check gives the user a shorter
      path to the fix.

    * Without containment, a path like `templates/evil.j2` where
      `templates` is a symlink to `/etc` would pass the lexical check
      and then read `/etc/evil.j2`. The containment check is what
      closes that hole.

`Path.relative_to` is used (rather than string prefix comparison)
because it handles all the edge cases correctly on case-insensitive
filesystems, handles normalisation, and raises `ValueError` for the
non-contained case — a single, well-defined failure that we can catch.

Why one list, not one error per check
-------------------------------------
The loader's contract is `Err(list[ValidationError])`. This module's
callers want every filesystem problem in the batch, not just the first.
So `check_templates` walks every declared file in every declared
location and accumulates errors, returning the full list. An empty list
means success.

A single entry produces at most one error. The check order
(safe → contained → exists → readable) is chosen so that the *first*
thing that can go wrong is what the user is told about. Running the
existence check on a path we have already refused to read would be
wasted work, and reporting both "escapes directory" and "file missing"
for the same entry would be confusing — the entry has one problem and
one fix.

Why `files` and `conditional_files` are handled together
--------------------------------------------------------
Both declare template references that must exist regardless of whether
the condition evaluates true — a conditional file that *would* be used
must be present, because the blueprint's author committed to it as part
of the deliverable. Deferring the check to condition-evaluation time
would push a load error into the rendering phase, which is exactly the
deferral this module prevents.

The path prefix distinguishes the two cases (`files.0.source` vs
`conditional_files.0.files.0.source`) so users can find the reference
in their YAML by structure, not by guessing which section it is in.

Empty blueprints are valid
--------------------------
A blueprint with no `files` and no `conditional_files` is a blueprint
whose rendering is entirely programmatic (or trivially empty). This
module returns `[]` for such a blueprint, not an error. Requiring at
least one file would be a policy decision that belongs in the schema
(`minItems: 1`), not in the filesystem checker.
"""

from __future__ import annotations

import os
from pathlib import Path

from forge.loading.errors import ValidationError
from forge.models import Blueprint

# Import the shared safety rule from `planning.paths` rather than
# re-implementing it here. ADR-0015 mandates exactly one definition of
# "is this path safe?" across the codebase; the loader and the planner
# are the two callers that must agree. A previous version of this
# module re-implemented the check inline, and the two drifted (the
# planner rejected NUL bytes, the loader did not) — the kind of
# divergence that produces a blueprint that validates but cannot be
# planned. Delegation makes that class of bug impossible.
#
# Why `planning.paths` and not `planning.destinations`:
#
# `is_safe_relative` used to live in `planning.destinations`, alongside
# `resolve()`. That module imports `TemplateRenderer`, which imports
# the rendering façade, which imports `forge.loading.result`, which
# triggers `forge.loading.__init__`, which imports `BlueprintLoader`,
# which imports *this module*. The cycle was:
#
#     loading.fs_checks → planning.destinations → rendering
#         → loading → loading.loader → loading.fs_checks
#
# which failed at import time with "cannot import name
# 'is_safe_relative' from partially initialized module".
#
# The rule itself has no rendering dependency; only `resolve()` does.
# Splitting them so the rule lives in a module with no rendering
# dependency (`planning.paths`) and the composition lives in a module
# that needs both (`planning.destinations`) breaks the cycle at the
# correct seam: the loader depends on the *rule*, not on the
# planner's composition machinery. `planning.paths` imports only
# `forge.planning.errors` (a pure dataclass module) and stdlib, so
# nothing this module imports can cycle back into it.
from forge.planning.paths import is_safe_relative


def check_templates(
    blueprint: Blueprint,
    *,
    blueprint_root: Path,
    source: str,
) -> list[ValidationError]:
    """Return one error per missing, unreadable, or escaping template.

    Parameters
    ----------
    blueprint
        A structurally valid blueprint. Guaranteed to have parsed
        cleanly through the schema and model phases — this function
        assumes `.files` and `.conditional_files` are well-typed
        sequences and does not re-validate their shape.
    blueprint_root
        The directory containing the blueprint file. Template paths are
        resolved relative to this directory. The loader passes
        `path.parent`, so a blueprint at `.forge/blueprints/my.bp/
        blueprint.yaml` resolves templates against `.forge/blueprints/
        my.bp/`.
    source
        Input label for error rendering (usually the blueprint's file
        path). Stamped onto every error this function emits.

    Returns
    -------
    list[ValidationError]
        Empty if every template exists and is readable. Otherwise, one
        error per problem, with a path that names the offending entry
        in the blueprint so the user can find it by structure.

    Notes
    -----
    Errors are appended in traversal order (static files first, then
    conditional files in declaration order). The loader preserves this
    order; users read errors in the order their file declares the
    references, which matches how they will fix them.

    A single `files` entry can produce at most one error (lexical
    safety failure precludes containment, existence, and readability
    checks; each subsequent check only runs if the previous passed).
    This keeps the report proportional to the number of distinct
    problems, not the number of checks.
    """
    errors: list[ValidationError] = []

    # Static file references. Path index is `files.<i>.source` so it
    # matches the JSON structure the user edits.
    for index, entry in enumerate(blueprint.files):
        _check_one(
            relpath=entry.source,
            path_prefix=f"files.{index}.source",
            blueprint_root=blueprint_root,
            source=source,
            sink=errors,
        )

    # Conditional file references. The path nests one level deeper to
    # reflect the YAML structure: conditional_files[i].files[j].source.
    for cond_index, conditional in enumerate(blueprint.conditional_files):
        for file_index, entry in enumerate(conditional.files):
            _check_one(
                relpath=entry.source,
                path_prefix=f"conditional_files.{cond_index}.files.{file_index}.source",
                blueprint_root=blueprint_root,
                source=source,
                sink=errors,
            )

    return errors


def _check_one(
    relpath: str,
    path_prefix: str,
    blueprint_root: Path,
    source: str,
    sink: list[ValidationError],
) -> None:
    """Validate a single template reference, appending errors to `sink`.

    Extracted so the safety, containment, existence, and readability
    checks live in one place and are exercised identically for static
    and conditional files. The `sink` pattern (rather than returning a
    list) keeps the caller readable: it is a sequence of
    `_check_one(...)` calls, not a sequence of `errors.extend(...)` calls
    with intermediate variables.

    Check order (each short-circuits on failure):

        1. Lexical safety — cheap, no filesystem access, catches typos
           and absolute paths before we try to resolve anything.
        2. Resolved containment — catches symlink escapes that lexical
           safety cannot see.
        3. Existence — is there actually a file at the resolved path?
        4. Readability — can the CLI open it?

    Ordering is chosen so that the first failure the user sees is the
    one that, once fixed, will most likely make the rest of the checks
    pass — an entry that fails safety cannot meaningfully fail
    "readable" in a way that helps the user.
    """
    # ------------------------------------------------------------------
    # Layer 1: lexical safety.
    #
    # Delegated to `is_safe_relative` so the loader and the planner
    # apply *identical* rules — this is what "one safety net" (ADR-0015)
    # means in code. The rules include: no absolute paths, no `..`/`.`
    # segments, no empty segments, no NUL bytes or control characters,
    # length within bounds.
    #
    # We report the check as "not safe" rather than enumerating which
    # specific rule failed, because the shared function returns a
    # boolean and threading the specific finding through would require
    # this module to know about `Finding`, which is the planner's error
    # type, not the loader's. The hint names the most common cause
    # (traversal) and points at the reference; the user fixes it in one
    # pass either way.
    # ------------------------------------------------------------------
    if not is_safe_relative(relpath):
        sink.append(
            ValidationError(
                path=path_prefix,
                message=f"template path is not safe: {relpath!r}",
                hint=(
                    "Template paths must be relative, must not contain "
                    "'..' or '.' segments, and must not be absolute. "
                    "Reference only files inside the blueprint's own "
                    "folder."
                ),
                source=source,
            )
        )
        return

    # ------------------------------------------------------------------
    # Layer 2: resolved containment.
    #
    # `is_safe_relative` passed, so the path is lexically well-formed.
    # But the filesystem can turn a safe-looking path into an escape:
    # `templates/evil.j2` where `templates` is a symlink to `/etc`.
    # `resolve()` follows symlinks, and `relative_to` fails if the
    # resolved target is outside the root.
    #
    # We resolve the root every iteration rather than once, because the
    # cost is negligible at blueprint scale (a dozen files) and passing
    # a pre-resolved root around would make the function signature
    # misleading — callers would wonder which form they should pass.
    # Resolving here is cheap, correct, and self-documenting.
    # ------------------------------------------------------------------
    root_resolved = blueprint_root.resolve()
    candidate = (blueprint_root / relpath).resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        sink.append(
            ValidationError(
                path=path_prefix,
                message=f"template path escapes the blueprint directory: {relpath!r}",
                hint=(
                    "Reference only files inside the blueprint's own "
                    "folder. To share a template, copy it into the "
                    "blueprint directory rather than pointing outside."
                ),
                source=source,
            )
        )
        return

    # ------------------------------------------------------------------
    # Layer 3: existence.
    #
    # `is_file()` (not `exists()`) because a directory at the target
    # path is just as unusable as a missing file, and its message would
    # otherwise be confusing ("cannot read: is a directory" from the
    # readability check that would run next).
    #
    # The error message names the raw `relpath` (not the resolved
    # absolute path) because that is what the user wrote and can search
    # for in their YAML. Reporting `/abs/path/...` would force them to
    # translate back to the relative reference they authored.
    # ------------------------------------------------------------------
    if not candidate.is_file():
        sink.append(
            ValidationError(
                path=path_prefix,
                message=f"missing template file: {relpath!r}",
                hint=(
                    "Copy or create the template file inside the "
                    "blueprint directory, or remove this reference. "
                    "Template paths are relative to the blueprint's "
                    "own folder."
                ),
                source=source,
            )
        )
        return

    # ------------------------------------------------------------------
    # Layer 4: readability.
    #
    # `os.access` is technically racy (permissions can change between
    # the check and the read) and on some platforms it reflects real
    # UID rather than effective UID. Both are acceptable here: the goal
    # is to catch the common case (file mode 000, restrictive ACLs,
    # mount gone read-only) at validation time with a clear message,
    # not to guarantee the subsequent read will succeed. The renderer's
    # own `OSError` handling is still needed as a backstop.
    #
    # `R_OK` is the only permission that matters — the renderer never
    # writes to a template.
    # ------------------------------------------------------------------
    if not os.access(candidate, os.R_OK):
        sink.append(
            ValidationError(
                path=path_prefix,
                message=f"template file is not readable: {relpath!r}",
                hint=(
                    "Check the file's permissions — the CLI needs read "
                    "access. On Unix systems, `chmod +r` usually fixes "
                    "this; on Windows, check the file's Properties → "
                    "Security tab."
                ),
                source=source,
            )
        )
