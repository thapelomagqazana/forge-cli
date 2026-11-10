"""Traversal attacks: ``..`` sequences in various shapes.

The check exists in ``forge.safety.paths.safe_resolve``. Each test
below proves that a *specific spelling* of traversal is rejected by
``path.escapes_root`` or ``path.absolute`` (both are rejections).

Why so many spellings: an attacker does not type ``../`` — they type
the exact string the target application forgot to normalise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import safe_resolve


@pytest.mark.parametrize(
    "candidate",
    [
        # The canonical form.
        "../../etc/passwd",
        # Prefixed with a safe-looking segment.
        "foo/../../etc/passwd",
        # Trailing traversal after a deep-looking path.
        "a/b/c/../../../../etc/passwd",
        # A single ``..`` from a subdirectory.
        "../secret.txt",
        # The bare ``..`` (would resolve to the parent of root).
        "..",
        # Traversal mixed with legitimate segments.
        "src/../../../tmp/x",
    ],
    ids=[
        "canonical",
        "prefixed-safe-segment",
        "deep-lookalike",
        "single-parent",
        "bare-parent",
        "mixed-legitimate",
    ],
)
def test_traversal_rejected(tmp_path: Path, candidate: str) -> None:
    """Every spelling of ``..`` traversal is refused.

    The finding code is asserted explicitly: ``path.escapes_root`` is
    the *only* correct code for these (``path.absolute`` would mean we
    mis-classified a relative path as absolute, hiding a bug). A future
    refactor that made traversal fail with the wrong code would be
    caught here.
    """
    resolved, finding = safe_resolve(candidate, tmp_path)
    assert resolved is None
    assert finding is not None
    assert (
        finding.code == "path.escapes_root"
    ), f"expected path.escapes_root for {candidate!r}, got {finding.code!r}"
