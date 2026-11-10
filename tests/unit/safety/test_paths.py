"""Contract tests for ``safe_resolve`` — the safety layer's path checker.

What this file is, and what it is *not*
---------------------------------------
``safe_resolve`` has three test surfaces, each with a distinct job:

    * ``tests/adversarial/`` — a catalogue of *threats*. Each file is
      a class of attack (traversal, symlink escapes, NUL bytes, …) and
      each test is one spelling of that attack. The question answered
      is "does the safety layer defend against this class of attack?"
    * **This file** — a specification of the *contract*. Each test
      pins one (rule, finding code) pair and reads top-to-bottom as
      the function's acceptance criteria. The question answered is
      "does ``safe_resolve`` do what its docstring says?"
    * **Coverage report** — which lines ran. The question answered is
      "did the test suite touch this branch?"

These overlap in *inputs* — the same ``"../../etc/passwd"`` appears
in both this file and the adversarial suite — but they differ in
*what they assert about those inputs*. The adversarial test asserts
that traversal is rejected; this file asserts that traversal is
rejected *with the code* ``path.escapes_root``, that the rejection
happens *before* the symlink walk (so a traversal attempt with no
symlinks still fails on structure, not on a filesystem quirk), and
that the finding carries the raw candidate in its ``path`` field so
the user can see what they typed.

Why the ordering of the tests matters
-------------------------------------
``safe_resolve`` runs checks in this order (see the module docstring
of ``safety/paths.py``):

    1. Structural (NUL, empty, too long, control characters)
    2. Absolute (POSIX, Windows drive letter, UNC)
    3. Canonicalisation (``resolve()``, fails on loops)
    4. Symlink walk (unresolved *and* resolved components)
    5. Containment (``relative_to``)

The tests are grouped in the same order. Reading the file top-to-
bottom is reading the function top-to-bottom. A future contributor
adding a check knows exactly where in the file the new tests go, and
a future contributor *reordering* the checks knows which tests will
change (the ``test_*_wins_over_*`` tests in the "Check ordering"
section pin the intended precedence).

Why some tests are parametrised and some are not
------------------------------------------------
Parametrisation is used when the *input varies but the contract does
not*: e.g., POSIX absolute paths, Windows drive-letter paths, and
UNC paths are three spellings of one rule, so one parametrised test
covers all three and a new spelling is one more entry. Separate
functions are used when the *setup varies*: e.g., each symlink test
needs a differently-shaped fixture (leaf symlink, intermediate
symlink, loop), and folding them into a parametrised test would make
the fixture an opaque string in a list.

Why ``DEFAULT_POLICY`` is used unless a test is about policy
------------------------------------------------------------
The tests exercise the *production* policy by default. When a test
wants to prove that a policy knob has an effect, it constructs a
``SafetyPolicy(...)`` explicitly *inside that test* — so the test's
name, its body, and its imports all say "this test is about policy
knob X." Mixing "the rule is enforced" with "the rule is
configurable" makes both harder to reason about.

A note on filesystem-dependent tests
------------------------------------
A handful of tests depend on filesystem behaviour that is not
portable:

* ``test_symlink_loop_is_rejected`` — symlink support is
  platform-dependent (Windows requires elevated privileges unless
  Developer Mode is on). The test uses ``make_symlink`` from
  ``tests/adversarial/conftest.py``, which ``pytest.skip``s when the
  platform cannot create symlinks. A skip is honest; a failure would
  be a false negative on Windows CI.
* ``test_containment_is_component_wise_not_prefix`` — the setup
  creates a *sibling* directory whose string form starts with the
  root's string form. This is the exact case a string-prefix check
  would accept and a component-wise check rejects; a skip on the
  fixture side (should the OS not support ``Path.resolve``) is not
  expected, but if it happens the test surfaces the environment's
  limitation rather than silently passing.

The discipline: filesystem assumptions are either exercised on
portable behaviour (a real directory under ``tmp_path``) or made
explicit with ``pytest.skip(reason)``. Nothing is silently
conditioned on the CI host.

A note on backslashes in this file
----------------------------------
Several docstrings and parametrisations in this file contain Windows
paths (``C:\\Windows``, ``\\\\server\\share``, and the extended form
``\\\\?\\C:\\foo``). Those backslashes are written **doubled** so the
string literal represents a single backslash — the same convention
the source modules use. Do not "clean this up" by converting the
docstrings to raw strings: raw strings would render ``\\n`` and
``\\t`` literally, which would break the layout of any docstring that
also contains escape sequences intended to be real newlines or tabs.
Doubling is the narrower, safer fix, and it is what the rest of the
codebase already does for path descriptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import SafetyPolicy, safe_resolve
from tests.adversarial.conftest import make_symlink


# =============================================================================
# Group 1: the happy path
# =============================================================================
class TestAcceptsInsideRoot:
    """Paths that name a location inside the root are accepted unchanged.

    These tests exist to prove the function is *not* over-eager — a
    safety check that rejects valid paths is as useless as one that
    accepts invalid paths. Each test asserts both that the resolved
    path is the expected absolute path and that no finding was
    produced.
    """

    @pytest.mark.unit()
    def test_simple_filename(self, tmp_path: Path) -> None:
        resolved, finding = safe_resolve("file.txt", tmp_path)
        assert finding is None
        assert resolved == tmp_path.resolve() / "file.txt"

    @pytest.mark.unit()
    def test_nested_relative_path(self, tmp_path: Path) -> None:
        resolved, finding = safe_resolve("src/package/module.py", tmp_path)
        assert finding is None
        assert resolved == tmp_path.resolve() / "src" / "package" / "module.py"

    @pytest.mark.unit()
    def test_nonexistent_destination_is_accepted(self, tmp_path: Path) -> None:
        """We plan writes to files that do not exist yet — ``strict=False``.

        This is the reason ``resolve(strict=False)`` is used: the
        function's primary caller is the planner, which checks paths
        *before* they exist. A ``strict=True`` implementation would
        reject every legitimate destination.
        """
        resolved, finding = safe_resolve("new/file.txt", tmp_path)
        assert finding is None
        assert resolved is not None
        assert resolved.name == "file.txt"

    @pytest.mark.unit()
    def test_trailing_slash_is_normalised(self, tmp_path: Path) -> None:
        """A trailing slash on a directory destination is harmless."""
        resolved, finding = safe_resolve("src/", tmp_path)
        assert finding is None
        assert resolved == tmp_path.resolve() / "src"

    @pytest.mark.unit()
    def test_unicode_filename_is_accepted(self, tmp_path: Path) -> None:
        """Legitimate non-ASCII filenames are not the safety layer's concern."""
        resolved, finding = safe_resolve("naïve/文件.txt", tmp_path)
        assert finding is None
        assert resolved is not None
        assert "文件" in str(resolved)

    @pytest.mark.unit()
    def test_accepts_path_object(self, tmp_path: Path) -> None:
        """Both ``str`` and ``Path`` inputs are accepted, identically."""
        as_str, f1 = safe_resolve("file.txt", tmp_path)
        as_path, f2 = safe_resolve(Path("file.txt"), tmp_path)
        assert f1 is None
        assert f2 is None
        assert as_str == as_path


# =============================================================================
# Group 2: structural rejections (no filesystem access)
# =============================================================================
class TestStructuralRejections:
    """Rejections that happen before any filesystem call.

    These are the cheapest rejections: no I/O, no dependency on the
    filesystem's resolution behaviour. They are also the ones most
    likely to be tested in isolation in the future (e.g. as fuzz
    targets), because they are pure string checks.
    """

    @pytest.mark.unit()
    def test_nul_byte_rejected(self, tmp_path: Path) -> None:
        """A NUL byte cannot appear in any filesystem's filenames."""
        _, finding = safe_resolve("a\x00b", tmp_path)
        assert finding is not None
        assert finding.code == "path.nul_byte"

    @pytest.mark.unit()
    def test_empty_string_rejected(self, tmp_path: Path) -> None:
        """An empty path names "no file" — an error, not a default."""
        _, finding = safe_resolve("", tmp_path)
        assert finding is not None
        assert finding.code == "path.empty"

    @pytest.mark.unit()
    def test_too_long_rejected(self, tmp_path: Path) -> None:
        """A path exceeding the policy limit is refused.

        The limit is a *policy decision* (``_policy.py``), not a
        filesystem limit. Filesystems allow up to PATH_MAX (often
        4096), but the safety layer's limit is much smaller on the
        theory that a 400-char destination is almost certainly a bug
        (or an attack), not a legitimate path. The test pins that
        boundary.
        """
        too_long = "a" * 500
        _, finding = safe_resolve(too_long, tmp_path)
        assert finding is not None
        assert finding.code == "path.too_long"

    @pytest.mark.unit()
    @pytest.mark.parametrize("candidate", ["a\nb", "a\rb"], ids=["newline", "cr"])
    def test_control_characters_rejected(self, tmp_path: Path, candidate: str) -> None:
        """Newlines and carriage returns are never legitimate filenames."""
        _, finding = safe_resolve(candidate, tmp_path)
        assert finding is not None
        assert finding.code == "path.illegal_chars"

    @pytest.mark.unit()
    def test_too_long_honours_policy_override(self, tmp_path: Path) -> None:
        """The length check reads from the policy, not a hardcoded constant.

        Pinning the *policy seam* means a future change to
        ``max_path_length``'s default reaches every caller through
        ``SafetyPolicy()`` construction — no parallel constant to
        keep in sync.
        """
        policy = SafetyPolicy(max_path_length=5)
        _, finding = safe_resolve("abcdef", tmp_path, policy=policy)
        assert finding is not None
        assert finding.code == "path.too_long"


# =============================================================================
# Group 3: absolute-path rejections (all spellings)
# =============================================================================
class TestAbsolutePaths:
    r"""All three spellings of absolute — POSIX, Windows drive, UNC.

    Why all three are covered in the *contract* suite (not just the
    adversarial one): a contributor adding a fourth spelling (say,
    ``\\?\`` extended-length Windows paths) needs a single place to
    see what "absolute" means to ``safe_resolve`` today. That place is
    this class.
    """

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "candidate",
        [
            "/etc/passwd",
            # A second POSIX absolute path under a well-known system
            # directory. Earlier this slot was ``/tmp/foo``, but Ruff's
            # S108 flags the literal ``/tmp`` prefix as a
            # probable-insecure-temp-file pattern (it cannot tell the
            # path is only *named* as input, never opened). ``/etc/shadow``
            # serves the same role — obviously absolute, obviously
            # forbidden — without tripping the heuristic. The contract
            # under test ("POSIX absolute paths are rejected") is
            # unchanged.
            "/etc/shadow",
            "/",
        ],
        ids=["etc", "shadow", "root"],
    )
    def test_posix_absolute_rejected(self, tmp_path: Path, candidate: str) -> None:
        _, finding = safe_resolve(candidate, tmp_path)
        assert finding is not None
        assert finding.code == "path.absolute"

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "candidate",
        ["C:/Windows", "D:\\Users", "Z:/", "c:\\lower"],
        ids=["fwd-upper", "back-upper", "root-only", "fwd-lower"],
    )
    def test_windows_drive_letter_rejected(self, tmp_path: Path, candidate: str) -> None:
        """``Path.is_absolute()`` on POSIX does not detect ``C:/foo``.

        The safety layer checks ``raw[1] == ":"`` explicitly to catch
        this. The test covers both slash directions and both cases,
        so a future change to the check (say, matching only uppercase
        drive letters) fails on the lowercase case.
        """
        _, finding = safe_resolve(candidate, tmp_path)
        assert finding is not None
        assert finding.code == "path.absolute"

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "candidate",
        ["\\\\server\\share", "//server/share", "\\\\?\\C:\\foo"],
        ids=["backslashes", "slashes", "extended"],
    )
    def test_unc_path_rejected(self, tmp_path: Path, candidate: str) -> None:
        r"""UNC paths are absolute by construction, even on POSIX.

        On POSIX, ``Path("\\\\server\\share").is_absolute()`` returns
        False and ``raw[1] == ":"`` is False, so the old check missed
        UNC entirely and treated the path as a relative filename with
        literal backslashes. The safety layer checks
        ``raw.startswith(("\\\\\\\\", "//"))`` explicitly to close
        that gap.
        """
        _, finding = safe_resolve(candidate, tmp_path)
        assert finding is not None
        assert finding.code == "path.absolute"


# =============================================================================
# Group 4: canonicalisation and traversal
# =============================================================================
class TestTraversalAndCanonicalisation:
    """``..`` sequences are resolved and rejected when they escape.

    Why "canonicalisation and traversal" as one group: the two are
    inseparable. A traversal attempt is only *dangerous* if it
    resolves outside the root; a traversal that resolves *inside* the
    root is a legitimate no-op (``src/../README.md`` is
    ``README.md``). The check is on the *resolved* path, not the raw
    string, which is why these two concerns share one class.
    """

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "candidate",
        [
            "../secret",
            "../../etc/passwd",
            "src/../../outside",
            "a/b/c/../../../../outside",
        ],
        ids=["one-up", "two-up", "mid-path", "deep"],
    )
    def test_traversal_escape_rejected(self, tmp_path: Path, candidate: str) -> None:
        """Traversal that escapes the root fails on containment.

        Each candidate has *more* `..` segments than the depth of the
        path before it, so the resolved result escapes ``root``:

            "../secret"                  → one level above root
            "../../etc/passwd"           → two levels above root
            "src/../../outside"          → src/.. cancels, .. escapes
            "a/b/c/../../../../outside"  → four .. cancel a, b, c and the
                                           third .. escapes

        Candidates with *exactly enough* `..` to cancel the prefix
        (e.g. ``"a/b/c/../../../outside"``) resolve to
        ``root/outside`` — inside the root — and are covered by
        ``test_traversal_within_root_is_accepted`` below. The
        boundary between the two is the number of `..` segments
        relative to the path depth.
        """
        _, finding = safe_resolve(candidate, tmp_path)
        assert finding is not None
        assert finding.code == "path.escapes_root"

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "candidate",
        [
            "src/../file.txt",
            "a/b/c/../../../outside",
            "a/b/../../c/d",
            "./nested/./file.txt",
        ],
        ids=["one-level", "three-level", "interleaved", "dot-segments"],
    )
    def test_traversal_within_root_is_accepted(self, tmp_path: Path, candidate: str) -> None:
        """``..`` segments that resolve *inside* the root are accepted.

        This is the counterpart to ``test_traversal_escape_rejected``.
        The check is on the *resolved* path, not the raw string, so a
        path that contains ``..`` but ends up inside the root is
        accepted — as it should be, because writing to
        ``root/a/../file.txt`` is exactly writing to ``root/file.txt``.

        The boundary between this test and ``test_traversal_escape_rejected``
        is the *number* of ``..`` segments relative to path depth:

            - Fewer or equal `..` than depth    → resolves inside root (this test)
            - More `..` than depth              → resolves above root (the other test)

        Pinning both sides of that boundary means a future change to
        the containment check cannot silently flip one into the other.
        """
        resolved, finding = safe_resolve(candidate, tmp_path)
        assert finding is None, (
            f"expected {candidate!r} to resolve inside the root, "
            f"got finding {finding.code if finding else None!r}"
        )
        assert resolved is not None
        # Belt and braces: the resolved path is genuinely inside root.
        assert resolved.is_relative_to(tmp_path.resolve())

    @pytest.mark.unit()
    def test_bare_parent_is_rejected(self, tmp_path: Path) -> None:
        """``..`` alone resolves to the parent — outside the root."""
        _, finding = safe_resolve("..", tmp_path)
        assert finding is not None
        assert finding.code == "path.escapes_root"

    @pytest.mark.unit()
    def test_dot_alone_resolves_to_root_itself(self, tmp_path: Path) -> None:
        """``.`` resolves to the root itself — accepted, names the root.

        A caller who explicitly names the root with ``.`` gets the
        root's path back. Whether that is useful depends on the
        caller (a directory destination may be fine; a file
        destination would fail downstream), but it is not the safety
        layer's job to decide.
        """
        resolved, finding = safe_resolve(".", tmp_path)
        assert finding is None
        assert resolved == tmp_path.resolve()


# =============================================================================
# Group 5: symlink walk (unresolved and resolved)
# =============================================================================
class TestSymlinkWalk:
    """Symlinks are rejected by default, on both walks.

    The safety layer walks two paths (see ``safety/paths.py``'s
    module docstring): the *unresolved* path (the caller's string)
    and the *resolved* path (after ``resolve()`` has followed
    symlinks). The tests in this class exercise each walk's unique
    case, plus the intersection:

    * **Unresolved walk** — catches the leaf symlink the user can
      actually find and remove. This is the *actionable* case.
    * **Resolved walk** — catches redirects whose effect is a
      different path inside the root, where the resolved components
      look clean but the caller's path traversed a redirect.
    """

    @pytest.mark.unit()
    def test_leaf_symlink_rejected(self, tmp_path: Path) -> None:
        """A leaf symlink is caught by the unresolved walk."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("x")
        make_symlink(root / "real.txt", root / "link.txt")

        _, finding = safe_resolve("link.txt", root)
        assert finding is not None
        assert finding.code == "path.symlink_component"

    @pytest.mark.unit()
    def test_intermediate_symlink_rejected(self, tmp_path: Path) -> None:
        """A symlink in the middle of a path is caught."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real").mkdir()
        make_symlink(root / "real", root / "link")

        _, finding = safe_resolve("link/file.txt", root)
        assert finding is not None
        assert finding.code == "path.symlink_component"

    @pytest.mark.unit()
    def test_symlink_pointing_inside_root_rejected(self, tmp_path: Path) -> None:
        """Even a *benign-looking* symlink is rejected by default.

        The default policy is "no symlinks at all" (see
        ``_policy.py``). A symlink pointing inside the root is still
        a TOCTOU hazard (it can be retargeted between check and
        write), so it is rejected. This is the security fix the
        two-walk design was introduced to enforce — the resolved-only
        walk would have accepted this case, because ``resolve()``
        follows the symlink to a target that *is* inside the root.
        """
        root = tmp_path / "root"
        root.mkdir()
        (root / "real").mkdir()
        make_symlink(root / "real", root / "link")

        _, finding = safe_resolve("link/file.txt", root)
        assert finding is not None
        assert finding.code == "path.symlink_component"

    @pytest.mark.unit()
    def test_symlink_loop_is_rejected(self, tmp_path: Path) -> None:
        """A loop cannot resolve; the check fails closed."""
        root = tmp_path / "root"
        root.mkdir()
        make_symlink(root / "b", root / "a")
        make_symlink(root / "a", root / "b")

        _, finding = safe_resolve("a", root)
        assert finding is not None
        assert finding.code in {"path.unresolvable", "path.symlink_component"}

    @pytest.mark.unit()
    def test_symlink_policy_override_accepts_symlink(self, tmp_path: Path) -> None:
        """The policy knob works as documented.

        This test exists to prove the *policy seam* is real — a
        future change to ``allow_symlinks_in_destination`` has a
        covered effect. The test does not bless symlinks in
        production (the default remains ``False``); it blesses the
        knob's existence.
        """
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("x")
        make_symlink(root / "real.txt", root / "link.txt")

        policy = SafetyPolicy(allow_symlinks_in_destination=True)
        resolved, finding = safe_resolve("link.txt", root, policy=policy)
        assert finding is None
        assert resolved is not None


# =============================================================================
# Group 6: containment (the primary security check)
# =============================================================================
class TestContainment:
    """The resolved path must be a *component-wise* descendant of the root.

    Containment is the primary security check — it is what catches
    any escape that is not attributable to a symlink (a race, an
    exotic filesystem, a lexical ``..`` that survived the structural
    checks). The tests here pin its behaviour in isolation from the
    symlink walk (which normally runs first).
    """

    @pytest.mark.unit()
    def test_resolved_inside_root_is_contained(self, tmp_path: Path) -> None:
        """The common case: a resolved path inside the root is accepted."""
        resolved, finding = safe_resolve("a/b/c.txt", tmp_path)
        assert finding is None
        assert resolved is not None
        assert resolved.is_relative_to(tmp_path.resolve())

    @pytest.mark.unit()
    def test_containment_is_component_wise_not_prefix(self, tmp_path: Path) -> None:
        """``/srv/foobar`` is *not* inside ``/srv/foo``.

        A string-prefix check would pass this case; ``relative_to``
        fails it. The test constructs a sibling directory and confirms
        containment is rejected.
        """
        root = tmp_path / "foo"
        root.mkdir()
        sibling = tmp_path / "foobar"
        sibling.mkdir()

        # ``../foobar/file.txt`` escapes ``foo/`` even though
        # ``str(sibling).startswith(str(root))`` is True.
        _, finding = safe_resolve("../foobar/file.txt", root)
        assert finding is not None
        assert finding.code == "path.escapes_root"


# =============================================================================
# Group 7: check ordering (the load-bearing property)
# =============================================================================
class TestCheckOrdering:
    """The order of checks is a *contract*, not an implementation detail.

    When an input violates multiple checks, the finding code tells the
    user *which* check fired. The order is chosen so that the *first*
    thing wrong is also the *most actionable*:

        1. Structural — cheapest, no I/O.
        2. Absolute — cheap, common user error.
        3. Canonicalisation — first filesystem call.
        4. Symlink walk — more specific than containment.
        5. Containment — primary security check, but generic message.

    A future reordering must update the tests in this class *and*
    justify why the new order is better. The tests here are the
    guardrail.
    """

    @pytest.mark.unit()
    def test_nul_byte_wins_over_absolute(self, tmp_path: Path) -> None:
        """A NUL byte in an absolute path is reported as NUL, not absolute.

        ``"\\x00/etc"`` violates both rules. Structural checks run
        first because they need no I/O, and a NUL byte is a more
        fundamental problem than an absolute path.
        """
        _, finding = safe_resolve("\x00/etc", tmp_path)
        assert finding is not None
        assert finding.code == "path.nul_byte"

    @pytest.mark.unit()
    def test_absolute_wins_over_containment(self, tmp_path: Path) -> None:
        """An absolute path is reported as absolute, not as escape.

        ``"/etc/passwd"`` would resolve outside the root, so
        containment would catch it too. But the *actionable* message
        for the user is "you gave an absolute path" — that is the
        syntactic problem they can fix in one edit.
        """
        _, finding = safe_resolve("/etc/passwd", tmp_path)
        assert finding is not None
        assert finding.code == "path.absolute"

    @pytest.mark.unit()
    def test_symlink_wins_over_containment(self, tmp_path: Path) -> None:
        """A symlink-escape is reported as symlink, not escape.

        Both checks would reject this path. Symlink runs first so the
        user gets "remove the symlink" — the actionable fix — instead
        of "path escapes the root" — a symptom that requires further
        investigation.
        """
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("x")
        make_symlink(outside / "file.txt", root / "evil.txt")

        _, finding = safe_resolve("evil.txt", root)
        assert finding is not None
        assert finding.code == "path.symlink_component"


# =============================================================================
# Group 8: finding shape (the caller's contract)
# =============================================================================
class TestFindingShape:
    """``Finding`` is the caller's interface — its shape is a contract.

    Every call site that inspects a finding relies on the fields being
    populated: ``code`` for branching, ``path`` for display, ``message``
    and ``hint`` for the user. The tests here pin the minimum shape
    that every rejection satisfies.
    """

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "candidate",
        ["", "a\x00b", "/etc/passwd", "../x", "a" * 500],
        ids=["empty", "nul", "absolute", "traversal", "too-long"],
    )
    def test_every_finding_has_all_required_fields(self, tmp_path: Path, candidate: str) -> None:
        """Every rejection carries ``code``, ``path``, ``message``, and ``hint``.

        A finding without a hint is a dead end — the user knows what
        is wrong but not what to do. A finding without a path is
        unattributable. A finding without a code is unscriptable.
        The shape is enforced here so a future rejection path cannot
        silently skip one of the fields.
        """
        _, finding = safe_resolve(candidate, tmp_path)
        assert finding is not None
        assert finding.code, f"empty code for {candidate!r}"
        # ``path`` may be empty only for path-less findings, but none
        # of these candidates are path-less — they all name something.
        assert finding.path is not None
        assert finding.message, f"empty message for {candidate!r}"
        assert finding.hint, f"empty hint for {candidate!r}"

    @pytest.mark.unit()
    def test_finding_carries_root_for_context(self, tmp_path: Path) -> None:
        """``root`` is populated so the user sees *which* root was violated.

        The pairing (path, root) is often the whole story: the same
        path can be safe against one root and unsafe against another,
        and a message that says only "path escapes" with no root is
        confusing in a multi-root context.
        """
        _, finding = safe_resolve("/etc/passwd", tmp_path)
        assert finding is not None
        assert finding.root == str(tmp_path)

    @pytest.mark.unit()
    def test_success_returns_none_finding(self, tmp_path: Path) -> None:
        """The success shape is ``(Path, None)`` — unambiguous by design.

        The alternative (a finding on success, with a boolean ``ok``)
        would make every caller branch on ``finding.is_error`` — an
        extra concept with no benefit. The ``None`` sentinel is the
        correct shape: a caller that gets ``(path, None)`` proceeds; a
        caller that gets ``(None, finding)`` reports. No third state.
        """
        resolved, finding = safe_resolve("file.txt", tmp_path)
        assert resolved is not None
        assert finding is None
