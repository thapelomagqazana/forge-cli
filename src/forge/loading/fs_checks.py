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
For every file the blueprint declares, three conditions must hold:

    * **Contained** — the resolved template path stays inside the
      blueprint's own directory. A blueprint that points at
      `../../etc/passwd` is a security problem, not just a
      configuration one; we refuse it explicitly rather than letting
      the renderer read arbitrary files.
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

    A single `files` entry can produce at most one error (containment
    failure precludes existence/readability checks, since they would
    be checking a path we have already refused). This keeps the report
    proportional to the number of distinct problems, not the number of
    checks.
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

    Extracted so the containment, existence, and readability checks live
    in one place and are exercised identically for static and conditional
    files. The `sink` pattern (rather than returning a list) keeps the
    caller readable: it is a sequence of `_check_one(...)` calls, not a
    sequence of `errors.extend(...)` calls with intermediate variables.
    """
    # ------------------------------------------------------------------
    # Resolve both paths once.
    #
    # Resolving the root every iteration is wasted work in a batch of
    # dozens of files. Resolving the candidate is required for both the
    # containment check and the existence check, so we do it up front.
    #
    # `strict=False` (the default) means a broken symlink resolves to its
    # declared target — which then fails `is_file()`. That is the correct
    # behaviour: we want to report "missing template" rather than
    # propagate a `RuntimeError` from `resolve()`.
    # ------------------------------------------------------------------
    root_resolved = blueprint_root.resolve()
    candidate = (blueprint_root / relpath).resolve()

    # ------------------------------------------------------------------
    # Containment check.
    #
    # `relative_to` raises `ValueError` if `candidate` is not under
    # `root_resolved`. This catches:
    #   * `../` traversal,
    #   * absolute paths that happen to fall outside the root,
    #   * symlinks pointing outside the root (because we resolved).
    #
    # We do NOT report "path escape" for the trivial case
    # `candidate == root_resolved` — that would mean a file reference
    # pointing at the blueprint directory itself, which is invalid
    # (it is not a file) but is better reported as "not a file" below.
    # ------------------------------------------------------------------
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        sink.append(
            ValidationError(
                path=path_prefix,
                message=(f"template path escapes the blueprint directory: " f"{relpath!r}"),
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
    # Existence check.
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
    # Readability check.
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
