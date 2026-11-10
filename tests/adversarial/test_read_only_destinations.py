"""Read-only and untraversable destination parents.

Two distinct permission bits matter on POSIX:

* **W_OK** on the parent directory — required to create the file entry.
* **X_OK** on the parent directory — required to traverse into it to
  reach the file.

Either missing means the write will fail. ``validate_destination``
checks both via ``os.access(parent, W_OK | X_OK)``. The tests below
pin each bit independently; a change that dropped one from the check
would let one permission-only failure slip through as "safe".
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from forge.safety import validate_destination


def test_read_only_parent_rejects_write(tmp_path: Path) -> None:
    """A parent directory with no write permission is a hard refusal."""
    if os.geteuid() == 0:  # pragma: no cover
        pytest.skip("root bypasses os.access; not meaningful as root")

    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)  # read + traverse, no write
    try:
        findings = validate_destination(ro / "file.txt", overwrite=False)
    finally:
        os.chmod(ro, stat.S_IRWXU)

    assert any(f.code == "destination.parent_not_writable" for f in findings)


def test_untraversable_parent_rejects_write(tmp_path: Path) -> None:
    """A parent directory without X_OK cannot be entered, so no write.

    The rule matters because ``os.access(W_OK)`` alone returns True for
    a directory with ``--w-------`` (write, no traverse), yet a write
    into it fails at the OS level. The safety layer checks both bits.
    """
    if os.geteuid() == 0:  # pragma: no cover
        pytest.skip("root bypasses os.access; not meaningful as root")

    wo_only = tmp_path / "wo"
    wo_only.mkdir()
    os.chmod(wo_only, stat.S_IWUSR)  # write, no read, no traverse
    try:
        findings = validate_destination(wo_only / "file.txt", overwrite=False)
    finally:
        os.chmod(wo_only, stat.S_IRWXU)

    assert any(f.code == "destination.parent_not_writable" for f in findings)


def test_writable_ancestor_allows_nested_destination(tmp_path: Path) -> None:
    """A destination whose *immediate* parent does not exist yet is allowed
    when the nearest existing ancestor is writable.

    This mirrors ``mkdir(parents=True)``: the write will create the
    missing directories, provided the existing ancestor permits it.
    """
    findings = validate_destination(
        tmp_path / "new" / "deeply" / "nested" / "file.txt",
        overwrite=False,
    )
    assert not any(f.code == "destination.parent_not_writable" for f in findings)
