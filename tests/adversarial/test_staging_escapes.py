"""Staging escapes — writes that try to leave the staging directory.

The staging area's ``_prepare`` rejects relpaths that resolve outside
the staging root. These tests pin the check with the *shapes* of
attack that would slip past a naive check:

* ``..`` traversal
* absolute paths
* a relpath that resolves to the staging root itself (the directory,
  not a file inside it)

Each test asserts that ``write_text`` / ``write_bytes`` raises
``ValueError`` — the staging area's contract is "programmer error,
raise", not "user error, return finding". A caller that bypasses
``safe_resolve`` and feeds a hostile relpath to staging gets a loud
failure, not a silently-outside write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import StagingArea


def test_traversal_inside_staging_raises(tmp_path: Path) -> None:
    """A ``..`` segment escapes the staging root; refused."""
    with (
        StagingArea.for_root(tmp_path) as stage,
        pytest.raises(ValueError, match="escapes staging"),
    ):
        stage.write_text("../../etc/passwd", "attack")


def test_absolute_path_inside_staging_raises(tmp_path: Path) -> None:
    """An absolute path is not a valid relpath for staging."""
    with (
        StagingArea.for_root(tmp_path) as stage,
        pytest.raises(ValueError, match="escapes staging"),
    ):
        stage.write_text("/etc/passwd", "attack")


def test_empty_relpath_raises(tmp_path: Path) -> None:
    """An empty relpath names the staging root itself; refused."""
    with (
        StagingArea.for_root(tmp_path) as stage,
        pytest.raises(ValueError, match="must be non-empty"),
    ):
        stage.write_text("", "attack")


def test_dot_relpath_names_the_root_and_is_refused(tmp_path: Path) -> None:
    """``"."`` resolves to the staging root itself; refused as a target."""
    with (
        StagingArea.for_root(tmp_path) as stage,
        pytest.raises(ValueError, match="staging root itself"),
    ):
        stage.write_text(".", "attack")


def test_nul_byte_in_relpath_raises(tmp_path: Path) -> None:
    """A NUL byte makes the OS filename API reject the write; staging
    rejects earlier, with a clearer message."""
    with (
        StagingArea.for_root(tmp_path) as stage,
        pytest.raises(ValueError, match="NUL"),
    ):
        stage.write_text("a\x00b", "attack")
