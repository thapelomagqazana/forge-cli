"""Overwrite backups — data preservation is the safety layer's job.

Two guarantees tested here:

1. **Backup is created before the file is replaced.** A subsequent
   write does not lose the original; the user can restore it.

2. **Backup creation is atomic.** If the backup copy fails partway
   through, *no* backup is left behind and no original is modified.
   A partial backup set is worse than no backup set.

These tests live in ``adversarial/`` because their *purpose* is to
prove data is not lost in the presence of failures — not to test the
happy path. A future regression that made backups best-effort rather
than atomic would break the guarantee the safety layer's ADR-0017
promises, and these tests are where that regression is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import backup_existing


def test_backup_preserves_original_and_contents(tmp_path: Path) -> None:
    """The backup exists, has the same content, and the original is untouched."""
    original = tmp_path / "file.txt"
    original.write_text("precious data")

    backups = backup_existing([original], timestamp="20240101T000000Z")

    assert len(backups) == 1
    assert backups[0].original == original
    assert backups[0].backup.exists()
    assert backups[0].backup.read_text() == "precious data"
    assert original.read_text() == "precious data"


def test_nonexistent_target_is_silently_skipped(tmp_path: Path) -> None:
    """A target that does not exist has nothing to back up — not an error."""
    backups = backup_existing([tmp_path / "does-not-exist.txt"], timestamp="20240101T000000Z")
    assert backups == []


def test_backup_failure_rolls_back_all(tmp_path: Path, monkeypatch) -> None:
    """If any backup fails, all backups created so far are removed.

    A partial backup set is a lie to the user — the caller is told the
    operation failed, and if it then aborts (as it should), the
    filesystem must be in the same state as at entry. This is what
    "atomic" means at the multi-file level: either every file backed
    up, or none.
    """
    a = tmp_path / "a.txt"
    a.write_text("A")
    b = tmp_path / "b.txt"
    b.write_text("B")

    import forge.safety.overwrite as ow

    real_copy2 = ow.shutil.copy2
    call_count = {"n": 0}

    def fail_on_second(src, dst, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated disk full")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(ow.shutil, "copy2", fail_on_second)

    with pytest.raises(OSError, match="simulated"):
        backup_existing([a, b], timestamp="20240101T000000Z")

    # No debris: the first backup was rolled back.
    assert list(tmp_path.glob("*.bak.*")) == []


def test_backup_survives_two_calls_with_same_timestamp(tmp_path: Path) -> None:
    """Two backups with the same timestamp overwrite each other.

    This is a *known limitation* — the timestamp is second-granularity,
    and two sequential backups within one second collide on the backup
    filename. The test documents this: the *latest* backup wins, which
    matches the user's last edit. If a future change needs to
    distinguish, the fix is sub-second precision, not a test rewrite.
    """
    f = tmp_path / "file.txt"
    f.write_text("v1")
    backup_existing([f], timestamp="20240101T000000Z")
    f.write_text("v2")
    backup_existing([f], timestamp="20240101T000000Z")

    backups = list(tmp_path.glob("file.txt.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "v2"
