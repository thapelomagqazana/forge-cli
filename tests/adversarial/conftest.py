"""Shared fixtures for adversarial tests.

Kept small on purpose. Each test builds its own scenario inline so a
reader sees the *whole* attack in one screen — a fixture that "sets up
an attack" hides the attack behind a name, and the point of these
tests is that the attack is legible.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


@pytest.fixture()
def readonly_dir(tmp_path: Path):
    """Create a read-only directory, restoring permissions on teardown.

    Linux lets root bypass ``os.access(W_OK)``; the safety layer's
    check uses ``os.access``, so this fixture is skipped when running
    as root (the check would pass through no fault of the code).
    """
    if os.geteuid() == 0:  # pragma: no cover - root CI environments only
        pytest.skip("read-only permission tests are not meaningful as root")

    d = tmp_path / "readonly"
    d.mkdir()
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    try:
        yield d
    finally:
        os.chmod(d, stat.S_IRWXU)


def make_symlink(target: Path, link: Path) -> None:
    """Create a symlink, skipping the test on platforms without support.

    Windows requires elevated privileges for symlinks (unless the
    developer-mode flag is set). A missing capability should skip, not
    fail — otherwise the suite reports false negatives on Windows.
    """
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
