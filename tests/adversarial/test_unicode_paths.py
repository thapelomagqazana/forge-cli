"""Unicode paths and homoglyph lookalikes.

Two concerns:

1. **Legitimate Unicode** (``naïve/文件.txt``) must be *accepted* —
   the safety layer must not overreach and reject all non-ASCII
   filenames.

2. **Homoglyphs** (``..`` using a fullwidth period, which looks like
   an ASCII ``..``) must not be *mistaken* for traversal. On most
   filesystems they are valid characters, so a file named with those
   characters is a normal file; the safety layer treats them as
   regular characters. The test asserts the *behaviour*
   (safe-relative, escapes-root, or rejected for a specific reason)
   rather than a specific code, because the correct answer depends
   on whether the resolved path *actually* traverses.

The invariant: Unicode does not create ambiguity. Either a path is
inside the root (accepted) or it is not (rejected). The safety layer
must not fail-open because a character *looks like* traversal but is
not, nor fail-*closed*-with-wrong-error because it assumed the
lookalike was traversal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.safety import safe_resolve

# The fullwidth full stop is U+FF0E. It *looks* like the ASCII period
# (U+002E), so a filename built from two of them reads on screen like
# the ASCII ``..`` traversal segment. On every supported filesystem
# it is a legal ordinary character, and the safety layer must judge
# what the filesystem reports, not what the glyph looks like.
_FULLWIDTH_DOT = "\uff0e"


@pytest.mark.parametrize(
    "candidate",
    [
        "naïve/文件.txt",
        "emoji-🙂.txt",
        # Latin alpha (U+0061) is used here rather than Greek alpha
        # (U+03B1), even though the original parametrisation used the
        # Greek character. Reason: RUF001 flags any Greek alpha in a
        # *string literal* as a probable typo — the rule cannot tell
        # that this test is deliberately exercising a Greek character.
        # The test's purpose is "a Greek-like path is accepted"; the
        # Latin alpha proves the same point (both are legal filename
        # characters) without the false positive. A separate test,
        # ``test_greek_alpha_in_path_is_accepted`` below, exercises
        # the actual Greek alpha via a constructed string so the
        # coverage of that character class is explicit rather than
        # implied.
        "Ω/a.txt",
        "ñandú/résumé.py",
        "日本語/コード.py",
    ],
    ids=["mixed-latin-cjk", "emoji", "greek-latin-lookalike", "accented", "cjk"],
)
def test_legitimate_unicode_is_accepted(tmp_path: Path, candidate: str) -> None:
    """Unicode filenames inside the root are not rejected by the
    safety layer — the rules are about *path structure*, not character
    class.
    """
    resolved, finding = safe_resolve(candidate, tmp_path)
    assert finding is None, f"legitimate unicode path {candidate!r} was rejected: {finding}"
    assert resolved is not None


def test_greek_alpha_in_path_is_accepted(tmp_path: Path) -> None:
    """A path containing Greek alpha (U+03B1) is accepted.

    This test exists as the deliberate companion to the
    ``greek-latin-lookalike`` parametrisation above. RUF001 flags the
    Greek alpha in a *literal* because it is a probable typo for the
    Latin ``a``; constructing the string via ``chr(0x3B1)`` keeps the
    character in the test while making the intent legible to the
    linter (there is no ambiguous glyph on the source line, so there
    is nothing to flag).

    The behaviour is identical to the Latin-alpha case: Greek alpha
    is a legal filename character on every supported filesystem, and
    the safety layer's rules are about path *structure*, not
    character class. The two tests together pin both sides of that
    distinction — the lookalike-as-Latin case (no false positive) and
    the actual-Greek case (no false rejection) — without either
    being a lint suppression.
    """
    greek_alpha = chr(0x3B1)  # U+03B1 GREEK SMALL LETTER ALPHA
    candidate = f"{greek_alpha}/x.txt"
    resolved, finding = safe_resolve(candidate, tmp_path)
    assert finding is None, f"Greek-alpha path {candidate!r} was rejected: {finding}"
    assert resolved is not None


def test_fullwidth_dot_lookalike_does_not_create_ambiguity(tmp_path: Path) -> None:
    """A fullwidth-dot segment is *not* an ASCII ``..`` traversal segment.

    On a POSIX filesystem the fullwidth period is a legal filename
    component; on some legacy filesystems the two might normalise,
    but the safety layer must judge what the filesystem reports, not
    what a user's eyes see. The test pins the invariant: whatever the
    resolved behaviour, it is one of the two *defined* outcomes
    (safe-inside, or rejected with ``path.escapes_root``), never
    "silently traverses".

    The candidate string is assembled from ``_FULLWIDTH_DOT`` rather
    than written as a literal fullwidth-period glyph in the source.
    Reason: RUF001/RUF002/RUF003 flag the glyph in literals,
    docstrings, and comments, respectively — the rule's whole purpose
    is to catch a fullwidth period that a reader might mistake for an
    ASCII period. Here the *test itself* is about that exact
    confusion, so suppressing the rule would be honest; constructing
    the string from the codepoint keeps the confusion visible in the
    variable name and the docstring's explanation without requiring a
    suppression. It also makes the test robust against accidental
    editor normalisation of the source file (an editor that
    "helpfully" converts fullwidth to ASCII would silently change the
    test's input, turning a homoglyph test into a traversal test).
    """
    # The candidate looks like ``src/../etc/passwd`` to a reader, but
    # each ``.`` is U+FF0E, not U+002E — a legal ordinary character.
    candidate = f"src/{_FULLWIDTH_DOT}{_FULLWIDTH_DOT}/etc/passwd"
    resolved, finding = safe_resolve(candidate, tmp_path)
    # Either the path is a well-formed relative path (safe) or the
    # platform normalised the fullwidth dots to ASCII and the
    # containment check caught it. Both are acceptable. What is *not*
    # acceptable: the path traversing and being accepted.
    assert finding is None or finding.code == "path.escapes_root"
    if finding is None:
        assert resolved is not None
        assert (
            tmp_path.resolve() in resolved.parents
            or resolved == tmp_path / "src" / f"{_FULLWIDTH_DOT}{_FULLWIDTH_DOT}" / "etc" / "passwd"
        )
