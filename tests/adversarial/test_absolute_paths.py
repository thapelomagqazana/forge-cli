"""Absolute paths are rejected outright, on both POSIX and Windows.

Why two forms: ``Path.is_absolute()`` on POSIX returns False for
``"C:/Windows"``, so the safety layer checks both explicitly. This
test pins both checks — removing either would let one platform's
attack through while the other's still passed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import safe_resolve


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",  # POSIX
        # A POSIX path under a well-known system directory. Earlier
        # used ``/tmp/foo``, but Ruff's S108 flags the literal ``/tmp``
        # prefix as a probable-insecure-temp-file pattern (it cannot
        # tell this test only *names* the path as an input to
        # ``safe_resolve``). ``/etc/hosts`` serves the same role —
        # obviously absolute, obviously outside any project root —
        # without tripping the heuristic. The contract this test
        # pins ("absolute paths are rejected") is unchanged.
        "/etc/hosts",  # POSIX, deeper
        "C:/Windows",  # Windows drive letter, forward slash
        "D:\\Users\\me",  # Windows drive letter, backslash
        "\\\\server\\share",  # UNC path
    ],
    ids=["posix-shallow", "posix-deep", "win-drive-fwd", "win-drive-back", "win-unc"],
)
def test_absolute_paths_rejected(tmp_path: Path, candidate: str) -> None:
    """Absolute paths (all forms) are refused with ``path.absolute``.

    The code is asserted, not just the rejection, so a future change
    that let traversal-coded rejection fire for an absolute path would
    be caught — that would mean the absolute-path check was silently
    removed and we were falling through to the escape check.
    """
    resolved, finding = safe_resolve(candidate, tmp_path)
    assert resolved is None
    assert finding is not None
    assert (
        finding.code == "path.absolute"
    ), f"expected path.absolute for {candidate!r}, got {finding.code!r}"
