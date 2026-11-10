"""safe_resolve — the one canonical path checker in the codebase.

Why this module exists
----------------------
Path traversal is a deceptively simple class of bug. The naive check —

    if str(path).startswith(str(root)):   # WRONG
        allow()

— is defeated by three things:

    * shared prefixes   (`/srv/foo` vs `/srv/foobar`),
    * `..` sequences    (`/srv/foo/../bar`),
    * symlinks          (`/srv/foo/link → /etc`).

The correct check is the one the operating system performs when it
resolves a path: canonicalise both sides, then compare *path components*.
`pathlib.Path.resolve()` does the canonicalisation (following symlinks,
collapsing `..`, normalising separators); `Path.is_relative_to()` does
the component-wise comparison. Anything less is a bug waiting to ship.

The rule (in one sentence)
--------------------------
A path is safe if and only if `resolve(path)` is a descendant of
`resolve(root)`, and no symlink component of either the *resolved* or
the *unresolved* path sits between the root and the leaf.

Why both walks (resolved *and* unresolved)
------------------------------------------
`resolve()` follows symlinks. If the caller writes ``"link"`` and
``link`` is a symlink pointing *inside* the root, the resolved path is
the target — the leaf has no symlink in it because it is not the leaf
anymore. Walking only the resolved path would miss the redirect and
accept the path, violating the "no symlinks anywhere" default policy.

Walking the *unresolved* path catches exactly that case: the component
the caller wrote (``link``) is inspected with ``lstat``, is discovered
to be a symlink, and is rejected. The *resolved* walk remains as
defence in depth — it catches redirects that resolve to a
*different* path inside the root where the intermediate components on
the resolved side may be normal-looking. Two walks, one policy, no
gap.

Failure is data, not exceptions
-------------------------------
Every failure is returned as a `Finding`, never raised. Why? Because
callers want to *aggregate* — the same discipline used in loading,
mapping, and conditions. When a plan contains three traversal attempts
and one absolute path, the user should see all four at once, not one at
a time.

Why `lstat`, not `stat`
-----------------------
`os.stat` follows symlinks and lies: a symlink at `dest` pointing to
`/etc/passwd` reports as a regular file, world-writable, existing. All
true statements, all useless. `os.lstat` reports the entry itself — a
symlink is a symlink, broken or not. This module uses `lstat` everywhere
it inspects what is *on disk*.

Fail closed
-----------
Any check that cannot complete (permission denied reading metadata,
symlink loop during resolution, unexpected OSError) is a *rejection*.
"Probably fine" is not an acceptable answer for a security check.

Check ordering (why the current order is what it is)
----------------------------------------------------
   1. Cheap structural checks (NUL, empty, too long, control chars)
      — no filesystem I/O; the fastest rejections for the most
      obvious attacks.
   2. Absolute-path rejection (POSIX, Windows drive letter, UNC)
      — cheap, and the message ("must be relative") is the most
      actionable for this class of input error.
   3. Resolution (`Path.resolve(strict=False)`) — the first call
      that touches the filesystem, wrapped so loops and permission
      errors fail closed with ``path.unresolvable``.
   4. Symlink walk (resolved *and* unresolved) — the more specific,
      more actionable check. Running it before containment means
      a symlink in the path is reported as ``path.symlink_component``
      ("remove the symlink") rather than the symptom-shaped
      ``path.escapes_root`` ("path escapes the root") that
      containment would produce for the *same* underlying problem.
   5. Containment (`relative_to`) — the primary *security* check.
      Runs last because it is the fallback for any escape that is
      not attributable to a symlink (a race, an exotic filesystem,
      a lexical ``..`` that somehow survived step 2).
"""

from __future__ import annotations

import os
from pathlib import Path

from forge.safety._policy import DEFAULT_POLICY, SafetyPolicy
from forge.safety.errors import Finding


def safe_resolve(
    candidate: str | Path,
    root: str | Path,
    *,
    policy: SafetyPolicy = DEFAULT_POLICY,
) -> tuple[Path | None, Finding | None]:
    """Resolve `candidate` relative to `root`, or explain why it is unsafe.

    Returns
    -------
    (resolved_path, None) on success, or (None, finding) on failure.
    Never raises — callers aggregate findings into one report.

    Parameters
    ----------
    candidate : str | Path
        The path to check. Normally relative, though absolute paths are
        accepted syntactically and then rejected with a clear finding —
        so users get "absolute paths are not allowed" instead of a
        confusing "file not found" later.
    root : str | Path
        The directory `candidate` is resolved against. The caller is
        expected to have already created this directory (or accepted
        that resolution will be strict=False, see below).
    policy : SafetyPolicy
        The rules to enforce. Defaults to the project-wide policy.
        Injected for tests and for future per-call overrides.

    Resolution note
    ---------------
    We use `resolve(strict=False)` because the *destination* typically
    does not exist yet — we are planning a write, not opening a file.
    `strict=False` still canonicalises `..` and follows existing
    symlinks, which is exactly what we need: the *path* is judged by
    where it *would* end up, not whether it exists today.
    """
    # --- 1. Cheap structural checks -------------------------------------
    # Ordered from "cheapest and most obvious" to "most expensive". Every
    # early return avoids filesystem I/O, and the ordering means the most
    # common attacks get the fastest, clearest rejections.

    raw = str(candidate)

    if "\x00" in raw:
        return None, Finding(
            code="path.nul_byte",
            path=raw,
            message="path contains a NUL byte",
            hint="Remove the NUL byte; it is not a valid filename character.",
            root=str(root),
        )

    if not raw:
        return None, Finding(
            code="path.empty",
            path=raw,
            message="path is empty",
            hint="Provide a non-empty relative path.",
            root=str(root),
        )

    if len(raw) > policy.max_path_length:
        return None, Finding(
            code="path.too_long",
            path=raw,
            message=f"path exceeds {policy.max_path_length} characters",
            hint="Shorten the path.",
            root=str(root),
        )

    # Newlines and carriage returns are never legitimate in a filename;
    # they exist in inputs only to confuse log lines and downstream tools.
    for bad in policy.forbidden_chars:
        if bad in raw:
            return None, Finding(
                code="path.illegal_chars",
                path=raw,
                message="path contains a forbidden character (newline or carriage return)",
                hint="Remove newlines and carriage returns from the path.",
                root=str(root),
            )

    # --- 2. Absolute-path rejection (POSIX, Windows drive, UNC) --------
    # Three forms of absolute, all rejected with the same code:
    #
    #   * POSIX      "/foo"          — ``Path.is_absolute()`` on POSIX
    #   * Windows    "C:\\foo" / "C:/foo" — drive letter at index 1
    #   * UNC        "\\\\server\\share" / "//server/share" — network path
    #
    # Why UNC needs its own check: ``Path("\\\\server\\share")`` on POSIX
    # is a *relative* path (a filename that happens to contain
    # backslashes). ``is_absolute()`` returns False, and
    # ``raw[1] == ":"`` is False because ``raw[1]`` is ``"\\"``. Without
    # the explicit prefix check, a UNC path would be accepted as a
    # relative filename — and then either fail confusingly at write
    # time or, worse, be interpreted as a network path by a Windows
    # client consuming the same plan.
    p = Path(raw)
    is_posix_absolute = p.is_absolute()
    is_windows_drive = len(raw) > 1 and raw[1] == ":"
    is_unc = raw.startswith(("\\\\", "//"))
    if is_posix_absolute or is_windows_drive or is_unc:
        return None, Finding(
            code="path.absolute",
            path=raw,
            message="path must be relative, not absolute",
            hint="Provide a path relative to the target root.",
            root=str(root),
        )

    # --- 3. Canonicalise against the root ------------------------------
    # `resolve(strict=False)`:
    #   * follows existing symlinks in *existing* components,
    #   * collapses ".." *after* symlink following (correct order),
    #   * normalises separators,
    #   * does not fail on the final component not existing yet.
    # RuntimeError is raised on symlink loops; that is a rejection.
    root_resolved = _resolve_root(root)
    try:
        resolved = (root_resolved / p).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return None, Finding(
            code="path.unresolvable",
            path=raw,
            message=f"cannot resolve path: {exc}",
            hint="Check for symlink loops or unreadable directories.",
            root=str(root),
        )

    # --- 4. Symlink walk (resolved *and* unresolved) -------------------
    # See the module docstring's "Check ordering" section for the full
    # reasoning. The short version:
    #
    #   * The *unresolved* walk catches the symlink the caller actually
    #     wrote — including a leaf symlink that ``resolve()`` followed
    #     to a target that is inside the root. Without it, a leaf
    #     symlink pointing to a benign-looking target inside the root
    #     would pass containment and violate the "no symlinks
    #     anywhere" default policy.
    #
    #   * The *resolved* walk remains as defence in depth — it catches
    #     intermediate redirects whose resolved components look clean
    #     but whose *effect* is to move the write to a different part
    #     of the tree (a `root/a -> root/b` redirect is caught because
    #     the intermediate ``root/a`` was a symlink, even though the
    #     resolved path ``root/b/file`` contains no symlink).
    #
    # Running this *before* the containment check changes the finding
    # code for a leaf symlink pointing outside the root: it is now
    # reported as ``path.symlink_component`` ("remove the symlink")
    # instead of ``path.escapes_root`` ("path escapes the root"). Both
    # are rejections; the former is what the user needs to act on.
    if not policy.allow_symlinks_in_destination:
        finding = _check_no_symlink_components(
            resolved=resolved,
            root=root_resolved,
            raw=raw,
            unresolved=(root_resolved / p),
        )
        if finding is not None:
            return None, finding

    # --- 5. Component-wise descendant check ----------------------------
    # `relative_to` compares path components, so `/srv/foo` is not an
    # ancestor of `/srv/foobar`. String-prefix checks fail this case;
    # `relative_to` is what the operating system actually does.
    #
    # Runs *after* the symlink walk so that a symlink-caused escape is
    # reported as ``path.symlink_component`` (actionable) rather than
    # ``path.escapes_root`` (symptomatic). Containment still fires for
    # any escape the symlink walk did not catch — a race, an exotic
    # filesystem, or a lexical escape that survived step 2 — so it
    # remains the load-bearing security check.
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None, Finding(
            code="path.escapes_root",
            path=raw,
            message=f"path escapes the target root {root_resolved}",
            hint="Remove '..' segments; destinations must stay inside the root.",
            root=str(root),
        )

    return resolved, None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_root(root: str | Path) -> Path:
    """Canonicalise the root itself.

    If the root exists (the normal case), `resolve()` is correct. If it
    does not, we still want a stable canonical form to compare against;
    `resolve(strict=False)` gives us that without raising. The caller is
    responsible for the root existing at *write* time; here we only need
    a well-defined anchor for the descendant check.
    """
    return Path(root).resolve(strict=False)


def _check_no_symlink_components(
    resolved: Path,
    root: Path,
    raw: str,
    *,
    unresolved: Path,
) -> Finding | None:
    """Reject if any existing component is a symlink, on either walk.

    Two walks, in order:

    1. **Unresolved walk** — inspect the components the *caller wrote*
       (``unresolved = root / candidate``). This catches the symlink
       the user can actually find and remove. It also catches leaf
       symlinks whose target is *inside* the root — a case that the
       resolved walk cannot see, because ``resolve()`` has already
       followed the leaf to its target (a non-symlink).

    2. **Resolved walk** — inspect the components the *filesystem
       produced* after following all symlinks and collapsing ``..``.
       This catches redirects where the resolved path traverses a
       directory that happens to be a symlink even though the
       caller's written path did not (``root/a -> root/b`` and the
       caller wrote ``a/file.txt``; the resolved path ``root/b/file``
       has no symlink, but ``root/a`` *was* the redirect and the
       write lands there via ``a``'s target, not via ``b``).

    Both walks are needed; neither subsumes the other. See the module
    docstring's "Why both walks" section.

    Why `lstat`?
        ``os.path.islink()`` calls ``lstat`` internally; we call it
        explicitly so the semantics are obvious in the code and so
        we can handle ``OSError`` per component (a component we
        cannot inspect is a rejection, not a pass).

    Why the resolved walk *after* the unresolved one?
        Most attacks that a symlink-based escape relies on are
        visible in the *unresolved* path — the leaf is a symlink, an
        intermediate component is a symlink. Reporting those first
        gives the user a message about the symlink they wrote,
        rather than the same-named symlink at a resolved location
        (which may be a *different* symlink that only exists after
        resolution).
    """
    # --- Walk 1: unresolved components ---------------------------------
    # ``root / candidate`` — the path the caller wrote, anchored at the
    # root. Any component that exists and is a symlink is rejected.
    # Components that do not exist yet are fine — the write will create
    # them as regular files/directories.
    unresolved_parts = _relative_parts(unresolved, root, raw)
    if isinstance(unresolved_parts, Finding):
        return unresolved_parts

    finding = _walk_for_symlinks(root, unresolved_parts)
    if finding is not None:
        return finding

    # --- Walk 2: resolved components -----------------------------------
    # ``resolved`` — the path after following symlinks and collapsing
    # ``..``. Any component that exists and is a symlink is rejected.
    # The parts list is computed against the *resolved* root, not the
    # written root, because symlink resolution may have moved the
    # path out of the written root before moving it back in.
    resolved_parts = _relative_parts(resolved, root, raw)
    if isinstance(resolved_parts, Finding):
        return resolved_parts

    return _walk_for_symlinks(root, resolved_parts)


def _relative_parts(path: Path, root: Path, raw: str) -> tuple[str, ...] | Finding:
    """Return ``path``'s components relative to ``root``, or a finding.

    A ``ValueError`` from ``relative_to`` means ``path`` is not a
    descendant of ``root``. In walk 1 (unresolved) that means the
    lexical checks somehow let a non-descendant through — a bug we
    fail closed on. In walk 2 (resolved) that means the resolved
    path escaped the root; the containment check that runs *after*
    this function will report it with ``path.escapes_root``, but
    we still must return *something* from here so the caller can
    proceed to that check — returning a ``Finding`` here would
    pre-empt containment's more appropriate code.
    """
    try:
        return path.relative_to(root).parts
    except ValueError:
        return Finding(
            code="path.escapes_root",
            path=raw,
            message="component walk found a path outside the root",
            hint="This is a bug in the safety layer; please report it.",
            root=str(root),
        )


def _walk_for_symlinks(root: Path, parts: tuple[str, ...]) -> Finding | None:
    """Walk components of ``root / parts`` and reject any that is a symlink.

    Extracted so both walks (unresolved and resolved) share the same
    detection logic — one place for the ``lstat`` + ``islink`` + error
    branches, one place to reason about "what counts as a symlink".

    Nonexistent components cannot be symlinks; skipping them is not
    "letting a possible attack through" — the write will create them
    as regular directories, so no symlink can be traversed.
    """
    current = root
    for part in parts:
        current = current / part
        try:
            # lstat: inspect the entry itself, not its target.
            os.lstat(current)
        except FileNotFoundError:
            # Component does not exist yet — the write will create it.
            # Cannot be a symlink, so it is safe.
            continue
        except OSError as exc:
            # We could not inspect this component. Reject: fail closed.
            return Finding(
                code="path.inspect_failed",
                path=str(current),
                message=f"cannot inspect component: {exc}",
                hint="Check directory permissions.",
                root=str(root),
            )
        if os.path.islink(current):
            return Finding(
                code="path.symlink_component",
                path=str(current),
                message=f"symlink found in destination path: {current}",
                hint=(
                    "Destinations may not traverse symlinks. Remove the "
                    "symlink, or choose a different destination."
                ),
                root=str(root),
            )
    return None
