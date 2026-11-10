"""Safety-layer unit tests.

Marking this directory a package changes pytest's import mode
for its contents from ``prepend`` to qualified names: the test
modules are imported as ``tests.unit.safety.test_paths``, not as
``test_paths``. That distinction is what allows a basename to be
used in more than one test directory (e.g. ``test_paths.py`` in
both ``tests/unit/safety/`` and ``tests/unit/planning/``) without
the two modules colliding in ``sys.modules``.

Without this marker, pytest imports each file by its *basename*,
and two directories with the same file (say, ``test_collisions.py``
in both ``tests/unit/safety/`` and ``tests/unit/planning/``) fail
at collection with ``import file mismatch``. The fix for a single
collision is to rename one of the files; the fix for the *class*
of collisions is to make both directories packages so the
namespace is fully qualified.

Every other directory under ``tests/unit/`` (``cli/``, ``core/``,
``models/``) already has this marker. ``safety/`` and
``planning/`` were the outliers; adding it here brings the layout
into alignment and closes the door on the class of bug that the
last run surfaced.

The two renamed test files (``test_safety_collisions.py``,
``test_safety_destinations.py``) exist because the *semantic*
distinction matters too: a reader of the file listing should be
able to tell at a glance that these test the safety layer's
collision/destination primitives, not the planning layer's
equivalents. The ``test_safety_`` prefix makes that distinction
visible at the basename level, where a reader first encounters
the file.
"""
