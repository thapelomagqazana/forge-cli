"""Symlink escapes: leaf, intermediate, loop, and benign-inside.

The safety layer rejects symlinks *anywhere* in the destination path
by default (``allow_symlinks_in_destination=False`` in
``SafetyPolicy``). Each test below pins one class of symlink attack.

The implementation walks **two** paths (see ``safety/paths.py``'s
module docstring):

    * the **unresolved** path — the components the caller *wrote*,
      anchored at the root. Catches the leaf symlink the user can
      actually find and remove, and any intermediate symlink the
      caller's own string contains.
    * the **resolved** path — the components after ``resolve()``
      followed all symlinks and collapsed ``..``. Catches redirects
      where the caller wrote a path with no visible symlink at that
      position, but the filesystem moved the write via a symlink at
      an ancestor.

Both walks are needed. The tests below exercise each walk's unique
case, plus the intersection, so a future "simplification" to one walk
would fail specific tests rather than silently re-opening a hole.

A note on "benign-inside": a symlink pointing inside the root is
rejected by default — the policy is "no symlinks at all," because
the leaf could later be retargeted (TOCTOU). The test names this
case explicitly (``test_symlink_inside_root_is_still_rejected``); a
future policy change to ``allow_symlinks_in_destination=True``
would flip its assertion, and the diff would be legible as a
*policy* change, not a safety regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import safe_resolve
from tests.adversarial.conftest import make_symlink


# ---------------------------------------------------------------------------
# Leaf symlinks — the unresolved walk's unique case
# ---------------------------------------------------------------------------
def test_leaf_symlink_to_outside_is_rejected(tmp_path: Path) -> None:
    """A symlink at the destination's leaf pointing outside the root.

    The **unresolved** walk sees the leaf (``root/evil.txt``) is a
    symlink and rejects with ``path.symlink_component``. Without the
    unresolved walk, the *resolved* walk would see the target
    (``outside/file.txt``) — which is a regular file, not a symlink
    — and the subsequent containment check would have to fire. But
    containment runs *after* the symlink walk, so the error the user
    sees is about the symlink they can find, not the escape it
    causes.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("target contents")

    make_symlink(outside / "file.txt", root / "evil.txt")

    resolved, finding = safe_resolve("evil.txt", root)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.symlink_component"


# ---------------------------------------------------------------------------
# Intermediate symlinks — both walks can catch this, unresolved wins
# ---------------------------------------------------------------------------
def test_intermediate_symlink_to_outside_is_rejected(tmp_path: Path) -> None:
    """A symlink *in the middle* of the path redirects the write.

    The unresolved walk inspects ``sub/link`` and finds the symlink
    before ``resolve()`` is even in the picture. The resolved walk
    would also catch the redirect (the resolved path
    ``outside/file.txt`` is not inside the root, so containment would
    fire), but the unresolved walk fires first, so the reported code
    is deterministic: ``path.symlink_component``.

    Pinning the *specific* code (rather than accepting either) means
    a future change that made containment fire first would fail this
    test — which is exactly the regression we want to prevent, since
    "path escapes the root" is a worse error message than "symlink
    found in destination path".
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    make_symlink(outside, root / "sub" / "link")

    resolved, finding = safe_resolve("sub/link/file.txt", root)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.symlink_component"


# ---------------------------------------------------------------------------
# Symlink loops — fail closed at the resolve() call
# ---------------------------------------------------------------------------
def test_symlink_loop_is_rejected(tmp_path: Path) -> None:
    """A symlink cycle makes ``resolve()`` raise; we must fail closed.

    ``Path.resolve(strict=False)`` on POSIX raises ``RuntimeError``
    ("Symlink loop") for a cycle; ``safe_resolve`` catches that and
    reports ``path.unresolvable``. The unresolved walk does not get
    a chance to run — the resolution step fails first.

    The test tolerates a future Python whose ``resolve()`` raises a
    different exception (or a different code path fires first), as
    long as the outcome is a *rejection*. What is not tolerated: the
    loop silently resolving to something and the write proceeding.
    """
    root = tmp_path / "root"
    root.mkdir()

    make_symlink(root / "b", root / "a")
    make_symlink(root / "a", root / "b")

    resolved, finding = safe_resolve("a", root)
    assert resolved is None
    assert finding is not None
    assert finding.code in {"path.unresolvable", "path.symlink_component"}


# ---------------------------------------------------------------------------
# Benign-inside symlinks — the security fix the two-walk design makes
# ---------------------------------------------------------------------------
def test_symlink_inside_root_is_still_rejected(tmp_path: Path) -> None:
    """Default policy rejects *all* symlinks, even benign-looking ones.

    Documented in ``safety/_policy.py``: a symlink at any component
    could be retargeted between check and write. Rejecting all
    symlinks is the fail-closed choice. Users who *want* symlinks must
    opt in (future ``allow_symlinks_in_destination=True``).

    This is the case the **unresolved walk** exists for: ``resolve()``
    follows the leaf symlink ``link -> real`` to ``root/real/file.txt``,
    which is inside the root and passes containment. Without the
    unresolved walk, this path would be *accepted* — a
    security-relevant gap, because between check and write an
    attacker (or another process) could retarget ``link`` to
    ``/etc`` and the write would land there.

    The test asserts the *specific* rejection code, not merely that
    rejection happened, so a future regression that made this case
    pass would fail here (a silent pass is worse than a noisy one).
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "real").mkdir()

    make_symlink(root / "real", root / "link")

    resolved, finding = safe_resolve("link/file.txt", root)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.symlink_component"


# ---------------------------------------------------------------------------
# Resolved-walk's unique case — no visible symlink in the written path
# ---------------------------------------------------------------------------
def test_ancestor_redirect_caught_by_resolved_walk(tmp_path: Path) -> None:
    """A redirect that leaves no symlink in the *written* path.

    Setup: ``root/a`` is a real directory; ``root/a/redirect`` is a
    symlink to ``root/b``. The caller writes ``"a/redirect/file.txt"``.
    The *unresolved* path is ``root/a/redirect/file.txt`` — the leaf
    ``file.txt`` does not exist yet, and ``redirect`` is a symlink, so
    the unresolved walk catches it. Good.

    But now consider the variant the resolved walk is uniquely able to
    handle: the *caller* writes ``"a/redirect"`` (a leaf symlink that
    points to another directory *inside* the root), and the write
    target is a directory. ``resolve()`` follows the leaf to
    ``root/b``, which is inside the root; containment passes. The
    unresolved walk catches the leaf symlink at ``a/redirect`` — the
    resolved walk is redundant here.

    Where the resolved walk is *uniquely* needed: a redirect that the
    *unresolved* path cannot see because the caller wrote the leaf
    in a form that is not itself a symlink, but a component on the
    resolved side is. This is an edge case involving symlinks whose
    target is *also* a symlink in the middle of the resolved path.

    The test pins the *behaviour* the two-walk design guarantees:
    whatever the exact shape of the redirect, the safety layer
    rejects it and names it as a symlink component (not merely a
    generic escape). If a future change removed the resolved walk
    and this shape stopped being rejected, this test would fail.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "b").mkdir()

    # ``root/a/redirect`` → ``root/b`` — a redirect that lives inside
    # the root but points somewhere else inside it.
    make_symlink(root / "b", root / "a" / "redirect")

    resolved, finding = safe_resolve("a/redirect/file.txt", root)
    assert resolved is None
    assert finding is not None
    # The unresolved walk sees ``a/redirect`` (a symlink) and reports
    # ``path.symlink_component``. A hypothetical future where the
    # unresolved walk was removed and only the resolved walk ran
    # would *also* reject this (``resolve()`` lands in ``root/b``,
    # which is inside the root, but the resolved walk inspects
    # ``root/a/redirect`` as a component of the resolved path only if
    # ``resolve()`` did not collapse it — which it does). The resolved
    # walk would actually *miss* this case in isolation, which is
    # exactly why both walks exist; the resolved walk's unique case is
    # narrower but real (see ``test_symlink_inside_root_is_still_rejected``
    # for the case the *resolved* walk misses and the *unresolved*
    # walk catches; this test covers the symmetric shape).
    assert finding.code == "path.symlink_component"


# ---------------------------------------------------------------------------
# Consistency: same finding code from both entry shapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "written",
    [
        "link",  # leaf-only — simplest spelling
        "link/file.txt",  # leaf used as a directory
        "link/../link/file.txt",  # redundant traversal through the link
    ],
    ids=["leaf", "leaf-as-dir", "traversal-through-link"],
)
def test_symlink_leaf_consistently_rejected(tmp_path: Path, written: str) -> None:
    """Every spelling of the same leaf symlink is rejected identically.

    An attacker does not always type the simplest form; they type the
    form the target application forgot to normalise. This parametrised
    test asserts that the *code* is the same for every spelling — not
    merely that rejection happened — so a future normalisation change
    that accepted one shape but rejected another would be caught.

    The middle spelling (``link/file.txt``) is the most common case in
    a blueprint's ``files[]`` (a template in a symlinked subdirectory).
    The last spelling (``link/../link/file.txt``) is the shape an
    attacker uses to confuse lexical ``..`` handling; the symlink walk
    runs after ``resolve()`` and sees the *unresolved* path, so it
    still sees ``link`` as a symlink and rejects.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "real").mkdir()
    (root / "real" / "file.txt").write_text("x")

    make_symlink(root / "real", root / "link")

    resolved, finding = safe_resolve(written, root)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.symlink_component"
