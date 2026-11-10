"""Adversarial test suite for the filesystem safety layer.

This package exists as its own directory (rather than living under
``tests/unit/safety/``) for two reasons:

1. **Different intent.** Unit tests check that a function does what
   its contract says. Adversarial tests check that a function *refuses
   to be attacked* — the inputs are chosen to break the safety
   guarantees, not to exercise the happy path. Mixing the two makes
   it hard to answer "how many distinct attacks does the safety layer
   defend against?" — the answer is the number of test functions in
   this package.

2. **Different lifecycle.** When a new CVE, a new filesystem quirk, or
   a new attack class is discovered, its test lands *here*. Unit-test
   files do not grow to accommodate threats; this directory does. A
   `git log tests/adversarial/` reads as a threat history.

The suite covers these classes (one file per class):

    * Traversal           — ``..`` sequences, mixed with safe segments
    * Absolute paths      — POSIX and Windows forms
    * Symlink escapes     — leaf, intermediate, loops, benign-inside
    * Unicode / homoglyph — paths that *look* like traversal but are not
    * NUL, length, control — invalid characters and length limits
    * Case-insensitive    — plan-internal and disk-side case conflicts
    * Read-only parents   — unwritable and untraversable ancestors
    * Staging escapes     — writes that try to leave the staging area
    * Overwrite backups   — that a backup is created and rollback works

Every test in this directory must:

    * **fail if its corresponding check is removed** — the point is to
      pin the check, not merely exercise the code path;
    * **use a minimal fixture** — a tmp_path and the smallest possible
      setup, so the attack is unambiguous;
    * **assert on the *specific* finding code**, not just "an error
      occurred" — a wrong-code pass hides regressions where a check
      fires for the wrong reason.
"""
