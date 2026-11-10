"""Invalid characters and length limits.

The rules:

* NUL bytes anywhere in a path are fatal (``path.nul_byte``).
* Newlines / carriage returns are fatal (``path.illegal_chars``).
* Paths longer than ``SafetyPolicy.max_path_length`` are rejected
  (``path.too_long``).

Each test pins one rule with a specific code, so a change to the rule
set would be caught at the code level, not only at the "rejected"
level.
"""

from __future__ import annotations

from pathlib import Path

from forge.safety import safe_resolve


def test_nul_byte_rejected(tmp_path: Path) -> None:
    """A NUL byte is not a valid filename character on any filesystem."""
    resolved, finding = safe_resolve("a\x00b", tmp_path)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.nul_byte"


def test_too_long_rejected(tmp_path: Path) -> None:
    """The length limit is a policy decision; the test pins it."""
    # ``max_path_length`` is 400 by default; use 500 to be safely over.
    resolved, finding = safe_resolve("a" * 500, tmp_path)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.too_long"


def test_newline_rejected(tmp_path: Path) -> None:
    """A newline in a path is never legitimate; it only confuses logs."""
    resolved, finding = safe_resolve("a\nb", tmp_path)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.illegal_chars"


def test_carriage_return_rejected(tmp_path: Path) -> None:
    """Same as newline, but the other character — separate test, separate
    reason for the rule to be checked independently."""
    resolved, finding = safe_resolve("a\rb", tmp_path)
    assert resolved is None
    assert finding is not None
    assert finding.code == "path.illegal_chars"
