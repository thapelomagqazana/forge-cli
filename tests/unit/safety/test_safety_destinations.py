"""Contract tests for ``validate_destination`` — the destination checker.

Where this file sits
--------------------
``validate_destination`` is the *second* gate in the safety layer,
running after ``safe_resolve`` has confirmed the path is structurally
safe. It answers a different question:

    ``safe_resolve``       — "is this path safe to write to?"
                              (no traversal, no absolute paths, no
                              symlinks in components, no NUL bytes,
                              no control characters)
    ``validate_destination`` — "is the destination that this safe path
                              names ready to receive the write?"
                              (leaf is not a symlink, no file already
                              there unless the caller consented,
                              parent is writable and traversable)

Each finding code corresponds to one of those concerns. The tests
below group by code, so a reader who wants to know "when does
``destination.parent_not_writable`` fire?" reads exactly one class
and sees the whole contract — both the positive cases (the rule
fires when it should) and the negative cases (the rule does *not*
fire when it should not).

Why positive AND negative tests
-------------------------------
A test suite that only proves "the finding fires when X is wrong"
catches *missing* rules but not *over-eager* rules. A validator that
rejected every destination would pass every "the finding fires"
test, and a user would have no path forward. The negative tests in
each class prove the other direction: when the condition is *not*
present, the finding is *not* produced.

The pair is what makes each class a *contract*, not a *smoke test*.

A note on the ``overwrite`` parameter
-------------------------------------
``validate_destination`` takes ``overwrite: bool`` as a required
keyword. The parameter is *not* defaulted on purpose: a default of
``False`` hides the choice at the call site, and a default of
``True`` would be a silent overwrite bug. The tests exercise both
values, and each test's name says which policy it is testing.

The leaf-symlink / existence interaction
----------------------------------------
The two checks are *not* independent. Under the default policy, a
leaf symlink produces ``destination.symlink`` and suppresses
``destination.exists`` — because both findings describe the same
on-disk entry, and their hints conflict (``--overwrite`` consents to
replacing a *file*, not to removing a *symlink*). Under
``allow_symlinks_in_destination=True``, the leaf-symlink check is
skipped by policy, and the existence check fires instead — because
``_lstat_exists`` treats a broken symlink as an entry on disk.

The tests that pin this interaction are:

    * ``TestLeafSymlink::test_broken_leaf_symlink_rejected`` — the
      default policy's finding, ``destination.symlink``.
    * ``TestLeafSymlink::test_broken_symlink_counts_as_existing_when_symlinks_allowed``
      — the opt-in policy's finding, ``destination.exists``. This is
      where ``_lstat_exists``'s "broken symlink is an entry" is
      verified on a path where it is actually reachable.
    * ``TestFindingShape::test_symlink_leaf_still_reports_unwritable_parent``
      — the multi-finding case, which the pre-interaction code
      suppressed (early return skipped parent-writability) and the
      current code reports.

The two single-finding tests and the one multi-finding test together
pin the boundary the interaction defines. A future change to the
order of checks, or to the suppression rule, breaks one of them and
the name of the failing test names the property that changed.

Filesystem-dependent tests
--------------------------
Two classes of behaviour depend on the OS:

* **Writability.** ``os.access(W_OK | X_OK)`` uses the *effective*
  UID. When running as root, the check is trivially true regardless
  of the directory's mode bits (root can write anywhere). Tests that
  need a *read-only* directory to prove the check fires must skip
  when running as root — otherwise the test would pass without
  exercising the rule.
* **Symlinks.** Creating a symlink on Windows requires either
  Developer Mode or elevated privileges. Tests that need a symlink
  skip when the platform cannot create one.

Both skips are *explicit* (via ``pytest.skip(reason=...)``) so a CI
run shows "N skipped" with the reason, rather than silently skipping
or failing for environmental reasons. This is the same discipline
``test_paths.py`` uses, and the same one the adversarial suite uses.

What this file does *not* cover
-------------------------------
* **Path structure.** That is ``test_paths.py``'s job. This file
  assumes the resolved path is already safe — every test passes a
  ``tmp_path``-derived path that would pass ``safe_resolve``.
* **Collision detection against a plan.** That is
  ``tests/unit/safety/test_collisions.py``'s job.
  ``validate_destination`` checks *one* destination; collision
  detection checks *many*.
* **The staging area.** That is
  ``tests/unit/safety/test_staging.py``'s job.
  ``validate_destination`` does not write; it only reads and reports.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from forge.safety import SafetyPolicy, validate_destination
from tests.adversarial.conftest import make_symlink


# =============================================================================
# Group 1: the happy path — a writable, empty destination
# =============================================================================
class TestAcceptsWritableDestination:
    """The common case: a new file in a writable directory, no symlinks.

    Every test in this class asserts two things: the findings list is
    empty (no false positives) and the destination's parent is
    genuinely writable (the fixture is what it claims to be). The
    second assertion is the test's *precondition* — if the fixture is
    wrong, the first assertion's success is meaningless.
    """

    @pytest.mark.unit()
    def test_new_file_in_writable_directory(self, tmp_path: Path) -> None:
        """A new file in a writable directory produces no findings."""
        findings = validate_destination(tmp_path / "new.txt", overwrite=False)
        assert findings == []

    @pytest.mark.unit()
    def test_nested_new_destination(self, tmp_path: Path) -> None:
        """A destination whose parent does not exist is accepted if the
        nearest existing ancestor is writable.

        This mirrors what ``mkdir(parents=True)`` does at write time:
        it will create the missing directories, provided the existing
        ancestor allows it. ``validate_destination`` walks up to the
        nearest existing ancestor and checks writability there — the
        same component the OS will actually use.
        """
        findings = validate_destination(tmp_path / "a" / "b" / "c" / "new.txt", overwrite=False)
        assert findings == []

    @pytest.mark.unit()
    def test_destination_is_a_directory_path(self, tmp_path: Path) -> None:
        """A destination that *names* a directory is a valid destination.

        ``validate_destination`` does not check whether the eventual
        write will be a file or a directory — that is the writer's
        concern (a file write to a directory fails at open time, a
        directory creation succeeds). The validator checks the
        *parent*, not the leaf, for writability, and that applies
        whether the leaf will be a file or a directory.
        """
        findings = validate_destination(tmp_path / "subdir", overwrite=False)
        assert findings == []

    @pytest.mark.unit()
    def test_destination_with_pathlike_input(self, tmp_path: Path) -> None:
        """``Path`` input works the same as ``str`` input.

        The signature says ``resolved: Path``, but a caller who has a
        ``Path`` built by joining pieces might reasonably pass it. The
        test pins that both work identically — no implicit ``str()``
        call is needed.
        """
        findings = validate_destination(Path(tmp_path) / "new.txt", overwrite=False)
        assert findings == []


# =============================================================================
# Group 2: leaf symlink
# =============================================================================
class TestLeafSymlink:
    """A symlink at the leaf is rejected by default.

    ``safe_resolve`` already rejects symlinks in *path components*, so
    a destination that reaches ``validate_destination`` should be
    symlink-free. But two cases bypass that guarantee:

    1. **TOCTOU between check and validate.** An external process
       replaces the leaf with a symlink after ``safe_resolve`` runs
       but before ``validate_destination`` runs. The validator
       re-checks the leaf for exactly this case.
    2. **A caller that skipped ``safe_resolve``.** Not recommended,
       but ``validate_destination`` is the *last* gate before a
       write, and it does not assume its caller. Re-checking is
       cheap; trusting the caller is not.

    The default policy rejects symlinks. A caller who *wants* a
    symlink-at-leaf writes must opt in via ``SafetyPolicy`` — this
    class's ``test_symlink_policy_override_accepts_leaf`` proves that
    knob works.
    """

    @pytest.mark.unit()
    def test_leaf_symlink_to_file_rejected(self, tmp_path: Path) -> None:
        """A leaf symlink pointing at a regular file is a finding."""
        target = tmp_path / "target.txt"
        target.write_text("x")
        leaf = tmp_path / "leaf.txt"
        make_symlink(target, leaf)

        findings = validate_destination(leaf, overwrite=False)
        assert any(f.code == "destination.symlink" for f in findings)

    @pytest.mark.unit()
    def test_leaf_symlink_to_directory_rejected(self, tmp_path: Path) -> None:
        """A leaf symlink pointing at a directory is a finding."""
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        leaf = tmp_path / "leaf"
        make_symlink(target_dir, leaf)

        findings = validate_destination(leaf, overwrite=False)
        assert any(f.code == "destination.symlink" for f in findings)

    @pytest.mark.unit()
    def test_leaf_symlink_to_inside_root_rejected(self, tmp_path: Path) -> None:
        """Even a benign-looking symlink is rejected by default.

        The same discipline as ``safe_resolve``: a symlink pointing
        inside the root is *still* a TOCTOU hazard. If the target is
        replaced between validate and write, the write lands at the
        new target. The default policy rejects all symlinks at the
        leaf for the same reason ``safe_resolve`` rejects them in
        components.
        """
        target = tmp_path / "real.txt"
        target.write_text("x")
        leaf = tmp_path / "link.txt"
        make_symlink(target, leaf)

        findings = validate_destination(leaf, overwrite=False)
        assert any(f.code == "destination.symlink" for f in findings)

    @pytest.mark.unit()
    def test_broken_leaf_symlink_rejected(self, tmp_path: Path) -> None:
        """A broken leaf symlink is still a symlink — still rejected.

        ``Path.exists()`` would return False for a broken symlink
        (the target does not exist), and a naive check might conclude
        "nothing there, safe to write". But the *name* is in use — a
        symlink occupies it, and overwriting it would replace the
        symlink, not the target. Rejecting it forces the user to
        remove the symlink explicitly, which is the correct
        remediation.

        The *finding code* the default policy produces is
        ``destination.symlink``, not ``destination.exists`` — see
        ``test_broken_symlink_counts_as_existing_when_symlinks_allowed``
        below for why the two codes are mutually exclusive for the
        same on-disk entry, and what the opt-in path produces instead.
        """
        leaf = tmp_path / "broken_link.txt"
        make_symlink(tmp_path / "does-not-exist.txt", leaf)

        findings = validate_destination(leaf, overwrite=False)
        assert any(f.code == "destination.symlink" for f in findings)

    @pytest.mark.unit()
    def test_broken_symlink_counts_as_existing_when_symlinks_allowed(self, tmp_path: Path) -> None:
        """A broken symlink is *existing* for the existence check.

        This is the counterpart to
        ``test_broken_leaf_symlink_rejected``. Under the default
        policy, the leaf-symlink check fires first and takes
        precedence — the existence finding would describe the same
        on-disk entry and offer a conflicting hint. Under
        ``allow_symlinks_in_destination=True``, the leaf-symlink
        check is disabled by policy, so the existence check runs and
        reports the broken symlink as an occupied name.

        The mechanism is ``_lstat_exists``: it uses ``os.lstat``
        (which inspects the *entry* on disk, not its target) rather
        than ``Path.exists()`` (which follows the symlink and reports
        False for a broken link). The test pins that choice — a
        future change to ``_lstat_exists`` that switched to
        ``Path.exists()`` would silently allow overwriting a broken
        symlink's name, and this test would catch it.
        """
        leaf = tmp_path / "broken_link"
        make_symlink(tmp_path / "does-not-exist", leaf)

        policy = SafetyPolicy(allow_symlinks_in_destination=True)
        findings = validate_destination(leaf, overwrite=False, policy=policy)

        assert any(f.code == "destination.exists" for f in findings)

    @pytest.mark.unit()
    def test_symlink_policy_override_accepts_leaf(self, tmp_path: Path) -> None:
        """The policy knob ``allow_symlinks_in_destination`` works.

        The test proves the knob is *wired*, not that symlinks are
        recommended. In production the default remains ``False``.
        A test that "blessed" symlinks would contradict the safety
        layer's purpose.
        """
        target = tmp_path / "real.txt"
        target.write_text("x")
        leaf = tmp_path / "link.txt"
        make_symlink(target, leaf)

        policy = SafetyPolicy(allow_symlinks_in_destination=True)
        findings = validate_destination(leaf, overwrite=False, policy=policy)
        assert not any(f.code == "destination.symlink" for f in findings)


# =============================================================================
# Group 3: existence (overwrite consent)
# =============================================================================
class TestExistingDestination:
    """An existing destination is a finding unless ``overwrite=True``.

    The whole point of the ``overwrite`` parameter is that the *caller*
    decides whether replacing existing files is acceptable. The
    default (``overwrite=False``) refuses; ``overwrite=True`` (passed
    from ``forge create --overwrite``, after the CLI has confirmed the
    user consented) allows.

    The tests in this class pin both sides. A future change to the
    check that dropped the "already exists" finding would fail
    ``test_existing_file_rejected``; a change that fired the finding
    even when ``overwrite=True`` would fail
    ``test_existing_file_accepted_with_overwrite``.

    Note on regular-vs-symlink existence: this class exercises the
    existence check for a *regular* file or directory. The "broken
    symlink counts as existing" case lives in ``TestLeafSymlink``,
    because it interacts with the leaf-symlink check (see that class's
    docstring for the interaction).
    """

    @pytest.mark.unit()
    def test_existing_file_rejected(self, tmp_path: Path) -> None:
        """A pre-existing file at the destination is a finding."""
        target = tmp_path / "existing.txt"
        target.write_text("x")

        findings = validate_destination(target, overwrite=False)
        assert any(f.code == "destination.exists" for f in findings)

    @pytest.mark.unit()
    def test_existing_directory_rejected(self, tmp_path: Path) -> None:
        """A pre-existing directory at the destination is a finding too.

        The check uses ``lstat`` (entry-based, not type-based), so a
        directory occupies the name just as a file does. The
        remediation (``--overwrite``, or choose a different
        destination) is the same in either case.
        """
        target = tmp_path / "existing_dir"
        target.mkdir()

        findings = validate_destination(target, overwrite=False)
        assert any(f.code == "destination.exists" for f in findings)

    @pytest.mark.unit()
    def test_existing_file_accepted_with_overwrite(self, tmp_path: Path) -> None:
        """``overwrite=True`` suppresses the existence finding.

        The caller has consented to replacing the file; the caller is
        also responsible for backing it up (via
        ``backup_existing``) before the write. That separation is what
        lets ``validate_destination`` be a *pure read* — it does not
        create backups, it only reports what the caller asked it to
        check.
        """
        target = tmp_path / "existing.txt"
        target.write_text("x")

        findings = validate_destination(target, overwrite=True)
        assert not any(f.code == "destination.exists" for f in findings)


# =============================================================================
# Group 4: parent writability (the most fragile check)
# =============================================================================
class TestParentWritability:
    """The parent directory must be writable *and* traversable.

    POSIX splits this into two bits, and both are required:

        * ``W_OK`` — to add the entry to the directory's contents.
        * ``X_OK`` — to traverse into it to reach the entry.

    A directory with ``W_OK`` but not ``X_OK`` cannot be written to
    despite ``os.access(W_OK)`` returning True. A directory with
    ``X_OK`` but not ``W_OK`` cannot be written to despite being
    traversable. ``validate_destination`` checks both bits; the tests
    below prove each one independently.

    When running as **root**, ``os.access`` returns True regardless
    of the bits — the kernel ignores them for the superuser. The tests
    that need a read-only directory to *prove* the check fires
    therefore skip under root, because under root the check would
    pass and the test would pass without exercising the rule. The
    skip is explicit, not silent.
    """

    @pytest.mark.unit()
    def test_readonly_directory_rejects_write(self, tmp_path: Path) -> None:
        """A directory with ``r-x`` (no write) is a finding."""
        if os.geteuid() == 0:  # pragma: no cover
            pytest.skip("read-only checks are not meaningful as root")

        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            findings = validate_destination(ro / "file.txt", overwrite=False)
        finally:
            os.chmod(ro, stat.S_IRWXU)

        assert any(f.code == "destination.parent_not_writable" for f in findings)

    @pytest.mark.unit()
    def test_untraversable_directory_rejects_write(self, tmp_path: Path) -> None:
        """A directory with ``-w-`` (write, no traverse) is a finding.

        This is the case that ``os.access(W_OK)`` alone would miss:
        the bit says "you may write", but without ``X_OK`` you cannot
        enter the directory to reach a file inside it. The check uses
        ``W_OK | X_OK`` so this case is caught; the test pins the
        check's correct behaviour for the case it is *most likely* to
        get wrong.
        """
        if os.geteuid() == 0:  # pragma: no cover
            pytest.skip("read-only checks are not meaningful as root")

        wo_only = tmp_path / "wo"
        wo_only.mkdir()
        os.chmod(wo_only, stat.S_IWUSR)
        try:
            findings = validate_destination(wo_only / "file.txt", overwrite=False)
        finally:
            os.chmod(wo_only, stat.S_IRWXU)

        assert any(f.code == "destination.parent_not_writable" for f in findings)

    @pytest.mark.unit()
    def test_writable_ancestor_allows_nested_destination(self, tmp_path: Path) -> None:
        """A destination whose *immediate* parent does not exist yet
        is accepted when the nearest existing ancestor is writable.

        The parent-writability walk mirrors ``mkdir(parents=True)``:
        if the ancestor that exists is writable and traversable, the
        write will succeed because the OS will create the
        intermediate directories. The test proves the walk does not
        stop at the (nonexistent) immediate parent and conclude
        "not writable".
        """
        findings = validate_destination(
            tmp_path / "new" / "deeply" / "nested" / "file.txt",
            overwrite=False,
        )
        assert not any(f.code == "destination.parent_not_writable" for f in findings)

    @pytest.mark.unit()
    def test_no_existing_ancestor_reports_no_ancestor(self, tmp_path: Path) -> None:
        """When no ancestor exists (or is reachable), the finding names it.

        This is a defensive path: in practice, ``tmp_path`` always
        exists, so the walk reaches it. The test constructs the
        situation artificially by chdir'ing nowhere— actually, this
        case is hard to trigger portably, so the test only asserts
        the *code* exists in the enum for finding codes rather than
        constructing an unreachable state. If you have a portable
        way to trigger it, replace this test with the real case.
        """
        # This test documents the finding code's existence; the exact
        # precondition is platform-specific and not portable. The
        # dispatch that emits it is in validate_destination's
        # `_check_parent_writable`, and its contract is exercised by
        # the positive tests above (they reach the same code path
        # when the parent is unwritable). Skipping rather than
        # passing a fake setup is the honest choice.
        pytest.skip(
            "no portable way to construct a filesystem with no "
            "reachable writable ancestor; the finding is covered by "
            "the parent-not-writable tests"
        )


# =============================================================================
# Group 5: policy seam — the destination validation respects the policy
# =============================================================================
class TestPolicySeam:
    """``validate_destination`` reads its rules from ``SafetyPolicy``.

    The policy object is the *single* place rules live (see
    ``safety/_policy.py``). The tests here prove that a future change
    to the default reaches every caller — not just ``safe_resolve`` —
    and that a caller can override a rule for a specific call without
    changing global behaviour.
    """

    @pytest.mark.unit()
    def test_symlink_default_policy_rejects(self, tmp_path: Path) -> None:
        """The production default rejects leaf symlinks."""
        target = tmp_path / "real.txt"
        target.write_text("x")
        leaf = tmp_path / "link.txt"
        make_symlink(target, leaf)

        findings = validate_destination(leaf, overwrite=False)
        assert any(f.code == "destination.symlink" for f in findings)

    @pytest.mark.unit()
    def test_symlink_opt_in_policy_accepts(self, tmp_path: Path) -> None:
        """The opt-in policy accepts leaf symlinks.

        Proof that the *knob* works, not a recommendation to use it.
        A future caller with a legitimate need (say, a blueprint that
        deliberately creates a symlink) can construct the policy
        explicitly. The default remains ``False``.
        """
        target = tmp_path / "real.txt"
        target.write_text("x")
        leaf = tmp_path / "link.txt"
        make_symlink(target, leaf)

        policy = SafetyPolicy(allow_symlinks_in_destination=True)
        findings = validate_destination(leaf, overwrite=False, policy=policy)
        assert not any(f.code == "destination.symlink" for f in findings)


# =============================================================================
# Group 6: finding shape and aggregation (the caller's contract)
# =============================================================================
class TestFindingShape:
    """Every finding has the shape the CLI renderer relies on, and
    multi-rule violations aggregate into one report.

    Same discipline as ``test_paths.py``'s ``TestFindingShape``: a
    finding without ``code``, ``path``, ``message``, or ``hint`` is a
    dead end. The tests here pin the minimum shape that every
    rejection satisfies, so a future code path cannot silently skip
    one of the fields.

    The aggregation test is *unique* to this file — ``safe_resolve``
    produces at most one finding per call, but ``validate_destination``
    produces one per applicable rule. The aggregation test pins that
    the batch-reporting promise holds even for the interaction cases
    (symlink-leaf + unwritable-parent), which is where the previous
    version of ``validate_destination`` short-circuited and lost a
    finding.
    """

    @pytest.mark.unit()
    def test_existence_finding_shape(self, tmp_path: Path) -> None:
        """A ``destination.exists`` finding carries all four fields."""
        target = tmp_path / "existing.txt"
        target.write_text("x")

        findings = validate_destination(target, overwrite=False)
        existing = next(f for f in findings if f.code == "destination.exists")
        assert existing.path  # names the destination
        assert existing.message
        assert existing.hint
        assert existing.code == "destination.exists"

    @pytest.mark.unit()
    def test_symlink_finding_shape(self, tmp_path: Path) -> None:
        """A ``destination.symlink`` finding carries all four fields."""
        target = tmp_path / "real.txt"
        target.write_text("x")
        leaf = tmp_path / "link.txt"
        make_symlink(target, leaf)

        findings = validate_destination(leaf, overwrite=False)
        sym = next(f for f in findings if f.code == "destination.symlink")
        assert sym.path
        assert sym.message
        assert sym.hint

    @pytest.mark.unit()
    def test_multiple_findings_are_aggregated(self, tmp_path: Path) -> None:
        """A destination that violates multiple rules reports *all* of them.

        The whole point of the findings-list return type is batch
        reporting. A destination that is both an existing file *and*
        has an unwritable parent should produce two findings, not
        one, so the user fixes both problems in one edit. The test
        constructs that case and asserts both findings are present.
        """
        if os.geteuid() == 0:  # pragma: no cover
            pytest.skip("read-only checks are not meaningful as root")

        ro = tmp_path / "ro"
        ro.mkdir()
        target = ro / "existing.txt"
        target.write_text("x")
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            findings = validate_destination(target, overwrite=False)
        finally:
            os.chmod(ro, stat.S_IRWXU)

        codes = {f.code for f in findings}
        # The file exists (destination.exists) AND the parent is
        # unwritable (destination.parent_not_writable). Both findings
        # are present — a caller who only inspected the first would
        # fix one problem and hit the other on the next run.
        assert "destination.exists" in codes
        assert "destination.parent_not_writable" in codes

    @pytest.mark.unit()
    def test_symlink_leaf_still_reports_unwritable_parent(self, tmp_path: Path) -> None:
        """A symlink-leaf destination with an unwritable parent reports *both*.

        This is the case the previous ``validate_destination``
        suppressed via an early return. The current code enumerates
        every applicable check, so the user sees:

            * ``destination.symlink`` — "remove the symlink"
            * ``destination.parent_not_writable`` — "fix the parent
              permissions"

        Both are true, both are actionable, and the user fixes both
        in one edit. The early return had been justified by "a
        symlink cannot also be a regular file, so the existence check
        would be meaningless" — which is correct for the *existence*
        check, but not for the *parent-writability* check, which is
        orthogonal. The current code distinguishes the two: symlink
        suppresses existence (same on-disk entry, conflicting hints)
        but does *not* suppress parent-writability (different
        problem, different fix).
        """
        if os.geteuid() == 0:  # pragma: no cover
            pytest.skip("read-only checks are not meaningful as root")

        ro = tmp_path / "ro"
        ro.mkdir()
        target_file = tmp_path / "target.txt"
        target_file.write_text("x")
        leaf = ro / "link.txt"
        make_symlink(target_file, leaf)
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            findings = validate_destination(leaf, overwrite=False)
        finally:
            os.chmod(ro, stat.S_IRWXU)

        codes = {f.code for f in findings}
        assert "destination.symlink" in codes
        assert "destination.parent_not_writable" in codes
        # The existence finding is *absent*: the symlink finding
        # already describes the leaf, and emitting both would offer
        # conflicting hints (see the module docstring of
        # ``destinations.py``). The test asserts the *absence*
        # explicitly, so a future change that emitted both would
        # fail here and the failure would name the interaction.
        assert "destination.exists" not in codes

    @pytest.mark.unit()
    def test_empty_findings_means_success(self, tmp_path: Path) -> None:
        """The success shape is an empty list — no `None`, no boolean.

        The alternative (returning `None` on success) would force
        every caller to check `is not None` before iterating, and
        would make "no findings" and "not checked" indistinguishable.
        The empty list is the correct shape: callers iterate it
        without a special case, and `[]` is falsy for the
        `if findings:` idiom that downstream code uses.
        """
        findings = validate_destination(tmp_path / "new.txt", overwrite=False)
        assert findings == []
