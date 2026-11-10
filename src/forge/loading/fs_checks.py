"""Filesystem checks that are part of *loading*, not scaffolding.

Why this module exists
----------------------
A blueprint can be structurally perfect — every required field present,
every type correct — and still be unusable because a template it
references does not exist on disk. Without this module, that failure
would surface during `forge create` as an `OSError` deep inside the
renderer, minutes after validation "succeeded" and with no path
information, no hint, and no way to catch it during a batch
`forge validate` run.

By treating missing templates as *validation errors* rather than
runtime failures, we get three properties that matter operationally:

    1. **Batched reporting** — a blueprint with three missing templates
       reports all three in one `Err([...])`, alongside any schema or
       model errors from other phases.
    2. **Actionable messages** — each error names the exact path in the
       blueprint (``files.0.source``) and the exact relative path that
       is missing, with a hint pointing at the fix.
    3. **Fail-at-load, not fail-at-create** — ``forge validate`` catches
       the problem in CI, before a user has committed a broken
       blueprint.

What is checked
---------------
For every file the blueprint declares, four conditions must hold, in
this order (each short-circuits on failure so one entry produces at
most one error):

    * **Lexically safe** — ``is_safe_relative`` rejects absolute paths,
      ``..``/``.`` segments, NUL bytes, control characters, and
      over-length paths. Cheap, no filesystem access, and the message
      names the syntactic problem directly.

    * **Contained** — the *resolved* template path stays inside the
      blueprint's own directory. This catches symlink escapes that the
      lexical check cannot see (``templates/evil.j2`` where
      ``templates`` is a symlink to ``/etc``).

    * **Exists** — the resolved path names an actual file, not a
      directory or a dangling symlink.

    * **Readable** — the CLI can open it (``os.R_OK``). A file that
      exists with mode ``000`` would fail during rendering; catching it
      here names permissions as the cause.

We do **not** read the file contents. Content problems (bad Jinja,
broken Python) belong to the renderer, which already has the machinery
to report them with template line numbers. Reading every template
twice — once here, once at render time — would double the cost and blur
the boundary between "this file exists and can be opened" and "this
file will parse as a template" — two very different questions with two
different failure modes.

Why the safety rule is imported from ``planning.paths``
------------------------------------------------------
ADR-0015 mandates exactly one definition of "is this path safe?" across
the codebase; the loader and the planner are the two callers that must
agree. A previous version of this module reimplemented the check inline,
and the two drifted (the planner rejected NUL bytes, the loader did
not) — the kind of divergence that produces a blueprint which validates
but cannot be planned. Delegation makes that class of bug impossible.

The rule used to live in ``planning.destinations`` alongside
``resolve()``. That module imports the renderer, which transitively
imports ``forge.loading``, which imports this module — a cycle that
failed at import time with "cannot import name 'is_safe_relative' from
partially initialized module". The rule has no rendering dependency;
only ``resolve()`` does. Splitting them so the rule lives in a module
with no rendering dependency (``planning.paths``) and the composition
lives in a module that needs both (``planning.destinations``) breaks
the cycle at the correct seam: the loader depends on the *rule*, not
on the planner's composition machinery.

Symlink handling
----------------
``Path.resolve()`` follows symlinks, and we use the resolved path for
both the containment check and the existence check. This is deliberate:

    * A symlink that points *inside* the blueprint directory is fine —
      it is a legitimate way to share a template between blueprints.

    * A symlink that points *outside* the blueprint directory is
      rejected by containment, exactly like a ``../`` path would be.
      Without resolving, a symlink named ``safe.j2`` pointing at
      ``/etc/passwd`` would bypass a purely lexical check.

    * A *dangling* symlink resolves to a path that does not exist, and
      is reported as "missing" — which is what the user needs to know.
      The message does not distinguish "broken symlink" from "missing
      file" because the fix is the same: create the target or remove
      the reference.

Empty blueprints are valid
--------------------------
A blueprint with no ``files`` and no ``conditional_files`` is a
blueprint whose rendering is entirely programmatic (or trivially
empty). This module returns ``[]`` for such a blueprint, not an error.
Requiring at least one file would be a policy decision that belongs in
the schema (``minItems: 1``), not in the filesystem checker.

Why ``files`` and ``conditional_files`` are handled together
------------------------------------------------------------
Both declare template references that must exist regardless of whether
the condition evaluates true — a conditional file that *would* be used
must be present, because the blueprint's author committed to it as part
of the deliverable. Deferring the check to condition-evaluation time
would push a load error into the rendering phase, which is exactly the
deferral this module prevents.

The path prefix distinguishes the two cases
(``files.0.source`` vs ``conditional_files.0.files.0.source``) so
users can find the reference in their YAML by structure, not by
guessing which section it is in.

What this module does *not* re-implement
----------------------------------------
    * The safety rules themselves — delegated to ``planning.paths``.
    * The containment logic (``Path.resolve`` + ``relative_to``) — that
      is a standard library idiom, not a bespoke check; it is spelled
      out here rather than extracted because extraction would obscure
      what the code does without reducing duplication (it appears
      exactly once in this module and once in ``safety.paths`` for a
      different purpose, with different error types — merging them
      would conflate two contracts).
    * Hint wording for the shared safety rule — the boolean form of
      ``is_safe_relative`` returns ``True``/``False``, so we supply a
      hint that names the most common cause (traversal). A future
      version may thread the *specific* safety finding through (the
      code already lives in ``planning.paths``); when it does, this
      module's message will get more precise without any signature
      change.
"""

from __future__ import annotations

import os
from pathlib import Path

from forge.loading.errors import ValidationError
from forge.models import Blueprint
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
        assumes ``.files`` and ``.conditional_files`` are well-typed
        sequences and does not re-validate their shape.
    blueprint_root
        The directory containing the blueprint file. Template paths are
        resolved relative to this directory. The loader passes
        ``path.parent``, so a blueprint at
        ``.forge/blueprints/my.bp/blueprint.yaml`` resolves templates
        against ``.forge/blueprints/my.bp/``.
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

    A single entry produces at most one error (see ``_check_one`` for
    the check ordering rationale). This keeps the report proportional
    to the number of distinct problems, not the number of checks.
    """
    errors: list[ValidationError] = []

    # Static file references. Path index is ``files.<i>.source`` so it
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
                path_prefix=(f"conditional_files.{cond_index}.files." f"{file_index}.source"),
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
    """Validate a single template reference, appending errors to ``sink``.

    Extracted so the safety, containment, existence, and readability
    checks live in one place and are exercised identically for static
    and conditional files. The ``sink`` pattern (rather than returning
    a list) keeps the caller readable: it is a sequence of
    ``_check_one(...)`` calls, not a sequence of ``errors.extend(...)``
    calls with intermediate variables.

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
    # Delegated to ``is_safe_relative`` so the loader and the planner
    # apply *identical* rules — this is what "one safety net"
    # (ADR-0015) means in code. The rules include: no absolute paths,
    # no ``..``/``.`` segments, no empty segments, no NUL bytes or
    # control characters, length within bounds.
    #
    # The boolean form loses the *specific* finding (which rule failed),
    # so we supply a hint that names the most common cause (traversal)
    # and the general class. The user fixes it in one pass either way;
    # a future version can thread the specific finding through without
    # changing this function's signature (the finding already lives in
    # ``planning.paths._safety_check``).
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
    # ``is_safe_relative`` passed, so the path is lexically
    # well-formed. But the filesystem can turn a safe-looking path into
    # an escape: ``templates/evil.j2`` where ``templates`` is a symlink
    # to ``/etc``. ``resolve()`` follows symlinks, and ``relative_to``
    # fails if the resolved target is outside the root.
    #
    # Root resolution is done once per call, not once per entry, but it
    # is written inline here (rather than hoisted) so that the *meaning*
    # — "compare against the resolved root, not the written root" — is
    # visible at the point of comparison. A future refactor that
    # hoisted it would still be correct; the current form makes the
    # intent obvious to a reader who only has this function in front of
    # them.
    #
    # The ``strict=False`` on root resolution (implicit in
    # ``.resolve()``'s default on most systems, but spelled out for
    # clarity) is important: the loader can be called with a
    # ``blueprint_root`` that does not exist yet (e.g. during a fresh
    # checkout of a blueprint repository, before ``git submodule
    # update``). Rejecting that earlier would produce a "root does not
    # exist" error before the more useful "template file is missing"
    # error.
    # ------------------------------------------------------------------
    root_resolved = blueprint_root.resolve()
    try:
        candidate = (blueprint_root / relpath).resolve()
    except (OSError, RuntimeError) as exc:
        # ``resolve()`` can raise ``OSError`` on permission errors
        # during the walk, or ``RuntimeError`` on symlink loops. Both
        # are rejections — fail closed, as everywhere in the loader.
        sink.append(
            ValidationError(
                path=path_prefix,
                message=f"cannot resolve template path: {relpath!r} ({exc})",
                hint=("Check for symlink loops or unreadable directories " "in the template path."),
                source=source,
            )
        )
        return

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
    # ``is_file()`` (not ``exists()``) because a directory at the
    # target path is just as unusable as a missing file, and its
    # message would otherwise be confusing ("cannot read: is a
    # directory" from the readability check that would run next).
    #
    # The error message names the raw ``relpath`` (not the resolved
    # absolute path) because that is what the user wrote and can search
    # for in their YAML. Reporting ``/abs/path/...`` would force them
    # to translate back to the relative reference they authored.
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
    # ``os.access`` is technically racy (permissions can change between
    # the check and the read) and on some platforms it reflects real
    # UID rather than effective UID. Both are acceptable here: the goal
    # is to catch the common case (file mode 000, restrictive ACLs,
    # mount gone read-only) at validation time with a clear message,
    # not to guarantee the subsequent read will succeed. The renderer's
    # own ``OSError`` handling is still needed as a backstop.
    #
    # ``R_OK`` is the only permission that matters — the renderer never
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
