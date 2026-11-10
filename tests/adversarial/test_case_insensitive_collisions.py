"""Case-insensitive collisions.

Two forms, in two places:

1. **Plan-internal duplicates** — the plan itself contains
   ``README.md`` and ``readme.md``. On macOS / Windows these are the
   same file, and the second silently overwrites the first. Detected
   by ``detect_collisions`` as ``plan.case_insensitive_duplicate``.

2. **Exact disk collisions** — the plan contains a destination that
   already exists on disk. This is *not* case-insensitive-specific:
   it fires on every filesystem. The test verifies the check still
   catches the exact match (which is the case the safety layer can
   guarantee on all platforms).

The safety layer deliberately does *not* try to catch disk
case-insensitive collisions, because it cannot know the target
filesystem. It catches plan-internal case duplicates, which it *can*
know.
"""

from __future__ import annotations

from pathlib import Path

from forge.planning import PlannedFile, PlannedFileKind
from forge.safety import detect_collisions


def _planned(dest: str) -> PlannedFile:
    return PlannedFile(source="s", destination=dest, kind=PlannedFileKind.COPY)


def test_plan_internal_case_duplicate_is_reported(tmp_path: Path) -> None:
    """``README.md`` + ``readme.md`` in the same plan is a defect.

    Reported as ``plan.case_insensitive_duplicate``. Not suppressed by
    ``overwrite=True`` — the flag consents to replacing *disk* files,
    not to a plan that will behave differently on different filesystems.
    """
    findings = detect_collisions(
        [_planned("README.md"), _planned("readme.md")],
        tmp_path,
        overwrite=False,
    )
    assert any(f.code == "plan.case_insensitive_duplicate" for f in findings)


def test_plan_internal_case_duplicate_not_suppressed_by_overwrite(tmp_path: Path) -> None:
    """``overwrite=True`` does *not* make the case-duplicate finding go away."""
    findings = detect_collisions(
        [_planned("README.md"), _planned("readme.md")],
        tmp_path,
        overwrite=True,
    )
    assert any(f.code == "plan.case_insensitive_duplicate" for f in findings)


def test_exact_disk_collision_reported(tmp_path: Path) -> None:
    """An existing destination is reported as ``collision.exists``."""
    (tmp_path / "file.txt").write_text("x")
    findings = detect_collisions([_planned("file.txt")], tmp_path, overwrite=False)
    assert any(f.code == "collision.exists" for f in findings)


def test_exact_disk_collision_suppressed_by_overwrite(tmp_path: Path) -> None:
    """``overwrite=True`` suppresses the disk-collision finding.

    The caller is expected to run ``backup_existing`` before writing,
    so the file is replaced but the data is preserved.
    """
    (tmp_path / "file.txt").write_text("x")
    findings = detect_collisions([_planned("file.txt")], tmp_path, overwrite=True)
    assert not any(f.code == "collision.exists" for f in findings)
