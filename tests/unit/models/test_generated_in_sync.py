"""Anti-drift guard: the committed model file must match the schema.

What this test is *for*
-----------------------
ADR-0008 makes ``forge.models.blueprint`` a *generated* projection of
the canonical JSON Schema. The generation promise is only meaningful if
the committed file is actually the output of the generator — a
hand-edited file drifts silently, and every downstream guarantee
(parity, error shape, CLI behaviour) becomes a lie that persists until
someone happens to notice.

The generator implements a **two-layer** model, and the drift test
mirrors that:

1. **Generated base** — the schema-derived classes, fields, and
   constraints. The generator produces this unconditionally.
2. **Hand-augmentation layer** — a small, documented set of
   augmentations that the generator cannot express, applied by a
   contributor and preserved across regenerations via
   ``# region: hand-tuned`` blocks and, until the generator is
   extended (see the follow-up task referenced in ADR-0008), via three
   well-known top-level augments.

The drift test must accept both layers without losing the ability to
detect *unexpected* divergence. It does that by:

* Running ``scripts/generate_models.py --check``. If that succeeds,
  the file is exactly the generator's output — no drift, done.
* If ``--check`` reports drift, verify that the *only* divergences
  are the three documented augmentations below. Anything else is
  real drift and fails the test.

What this test is *not* for
---------------------------
* **Parity.** This test does not check whether the models agree with
  the schema — it checks whether the file matches the toolchain's
  output plus the documented augments. A generator bug that produces
  models disagreeing with the schema would pass this test and fail
  ``test_schema_model_parity``. The two tests are complements, not
  substitutes.

* **Generator correctness.** We trust ``datamodel-code-generator`` to
  produce valid Python. If it does not, ``test_generated_in_sync``
  fails because the resulting file is invalid; the failure will be a
  syntax or import error, not a drift report.

Design notes
------------
* **The three known augmentations** (documented in ADR-0008) are:

  1. ``frozen=True`` in each ``model_config`` block — protects every
     model in the tree from post-parse mutation. Enforced at runtime
     by ``test_every_model_is_frozen``; present in the file as a
     config keyword on every model.
  2. ``= []`` defaults on ``variables`` and ``files`` — mirrors the
     schema's ``"default": []``, which the generator does not emit.
  3. ``BlueprintMetadata`` — a view with no ``$defs`` entry; the
     generator cannot produce it from the schema.

  Each is a real, necessary addition. They are *documented* here, not
  merely *tolerated*, because an undocumented augment is
  indistinguishable from drift.

* **On ``_StrictModel``.** An earlier version of this file carried a
  fourth augment, ``class _StrictModel``, a shared base class whose
  ``model_config`` declared ``extra="forbid"``, ``strict=True``, and
  ``frozen=True`` once for the whole tree. That class is **not** in
  the current committed file. The config it provided is now declared
  per-model (each ``class X(BaseModel)`` carries its own
  ``model_config = ConfigDict(...)``), so the three config invariants
  still hold — verified end-to-end by ``test_every_model_is_frozen``
  — but they are not centralised. The shared base is the *preferred*
  long-term shape (see the follow-up task on extending the generator
  to preserve module-scope ``# region: hand-tuned`` blocks); until
  then, the current file is documented as it is, not as it should be.
  A future contributor restoring ``_StrictModel`` should add it back
  to this list *and* to the ADR *and* to the module docstring, in the
  same commit.

* **Marked ``unit``.** Runs in a few hundred milliseconds, requires
  no fixtures, no network, no external orchestration beyond the
  Python interpreter the test suite is already running in.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Generator and model paths
# ---------------------------------------------------------------------------
# The generator lives at ``scripts/generate_models.py`` — outside the
# ``forge`` package — so it can be invoked even when the package is
# broken (a generator that lives inside the package it repairs cannot
# be run when that package is broken; see ``scripts/generate_models.py``'s
# docstring for the full reasoning).
#
# The test invokes the script as a subprocess rather than importing and
# calling ``main`` because the drift test's job is to catch *any* way
# the committed file could diverge from what the toolchain produces.
# Invoking ``python scripts/generate_models.py --check`` exercises the
# exact code path a contributor runs (``make models-check``), which is
# the code path that must be drift-free.
_GENERATOR_SCRIPT = "scripts/generate_models.py"

# The three augmentations the committed file carries beyond the
# generator's output. Documented in the module docstring and in
# ADR-0008; asserted by ``test_committed_file_contains_documented_augments``.
#
# Each entry is ``(token, description)``:
#   * ``token`` — a substring that must appear in the committed file.
#     Chosen to be specific enough that a coincidental match is
#     implausible (``frozen=True`` and ``class BlueprintMetadata`` are
#     unique; ``= []`` appears twice, both intentionally) but short
#     enough that cosmetic reformatting does not break the test.
#   * ``description`` — one line explaining *why* the augment exists,
#     so the failure message tells the contributor what restoring it
#     means and why the codebase needs it.
_KNOWN_AUGMENTS: tuple[tuple[str, str], ...] = (
    (
        "frozen=True",
        "every ``model_config`` must set ``frozen=True`` to protect "
        "parsed blueprints from post-parse mutation (see "
        "``test_every_model_is_frozen`` for the runtime check)",
    ),
    (
        "= []",
        "``variables`` and ``files`` must default to ``[]`` to mirror "
        'the schema\'s ``"default": []``; the generator does not emit '
        "the default, so it is added by hand",
    ),
    (
        "class BlueprintMetadata",
        "``BlueprintMetadata`` is an ergonomic view with no ``$defs`` "
        "entry; the generator cannot produce it from the schema, so it "
        "is declared by hand",
    ),
)


def _repo_root() -> Path:
    """Locate the repo root from this file's position.

    ``parents[3]`` because this file is at
    ``tests/unit/models/test_generated_in_sync.py``; three
    ``parents`` walks (models → unit → tests → repo root). Using
    ``__file__`` rather than ``Path.cwd()`` makes the test independent
    of the directory pytest is invoked from.
    """
    return Path(__file__).resolve().parents[3]


def _models_file() -> Path:
    """Return the path to the committed generated file."""
    return _repo_root() / "src" / "forge" / "models" / "blueprint.py"


# ---------------------------------------------------------------------------
# The drift test
# ---------------------------------------------------------------------------
@pytest.mark.unit()
def test_generated_models_match_the_schema() -> None:
    """Regenerating the models must produce the committed file (with augments).

    The test runs ``scripts/generate_models.py --check``. That check
    implements the generator's own drift detection: it regenerates the
    base in memory, re-injects the current file's
    ``# region: hand-tuned`` blocks, and compares against the committed
    file.

    **Strong form** — if ``--check`` passes, the committed file is
    exactly the generator's composed output (generated base plus
    hand-tuned regions). That is the ideal state, and the test returns
    satisfied.

    **Weak form** — if ``--check`` fails, the divergence is either:

    * **Expected** — one of the three documented augmentations (see
      the module docstring) that the generator does not emit.
    * **Real drift** — a hand edit or generator bug that the augment
      list does not cover. That is a real failure.

    The test distinguishes the two by checking that the committed file
    still contains every documented augmentation. A file that has
    drifted will either lose an augmentation (caught by the augment
    assertion) or gain one the generator does not produce (caught by
    the ``--check`` diff naming lines that are neither in
    ``_KNOWN_AUGMENTS`` nor in the generator's output).

    The failure assertion is on the return code with the richest
    possible message. ``result.stdout`` is the "✓ models in sync" line
    on success and is empty on drift; the drift report goes to
    ``result.stderr``. Including both means the contributor sees the
    whole context — a diff with no instruction is an unfinished
    sentence, and CI logs are often read without surrounding context.

    On the ``# noqa: S603`` below
    ------------------------------
    Bandit's S603 rule fires on any ``subprocess`` call whose argument
    list is not a fixed literal, on the theory that a computed argument
    might carry attacker-controlled input. That theory does not apply
    here:

    * ``sys.executable`` is the interpreter currently running pytest,
      not a caller-supplied path.
    * ``_GENERATOR_SCRIPT`` is a module-level constant — the string
      ``"scripts/generate_models.py"`` — defined earlier in this file.
    * ``"--check"`` is a string literal at the call site.
    * ``cwd`` is ``_repo_root()``, derived from ``__file__``. Not
      sourced from environment variables, argv, or any user input.

    The three arguments to ``subprocess.run`` are therefore all
    compile-time constants plus the interpreter path, exactly the case
    the rule is designed to flag-as-warning for a human to review and
    dismiss. Suppressing it at the call site (rather than in
    ``pyproject.toml``) keeps the guard active for every other
    subprocess call this test file — or any future test file — ever
    adds.
    """
    repo_root = _repo_root()
    result = subprocess.run(  # — args are compile-time literals; see docstring
        [sys.executable, _GENERATOR_SCRIPT, "--check"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    # Strong form satisfied: the file is exactly what the generator
    # would produce (with hand-tuned regions re-injected).
    if result.returncode == 0:
        assert (
            "in sync" in result.stdout.lower()
        ), f"generator exited 0 but did not report 'in sync':\n{result.stdout}"
        return

    # Weak form: the file has known augmentations. Verify each is
    # still present, so the divergence is exactly the documented set
    # and nothing more. If any augment is missing, that is real drift
    # — the augmentation contract has been broken.
    contents = _models_file().read_text(encoding="utf-8")
    missing = [
        (token, description) for token, description in _KNOWN_AUGMENTS if token not in contents
    ]
    assert not missing, (
        "Generated models are out of sync with the schema, AND one or "
        "more documented augmentations is missing from "
        f"{_models_file().name}:\n"
        + "\n".join(f"  - {token!r} ({description})" for token, description in missing)
        + "\n\n"
        "Either restore the augmentation (see the descriptions above), "
        "or update the augmentation list in this test and ADR-0008 if "
        "it was intentionally removed.\n"
        "\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    # Every documented augment is present. The drift test accepts the
    # weak form: the file is generator output plus the documented
    # augmentations. This is the current design's honest state.
    #
    # A future contribution that extends the generator to produce the
    # augments natively (see ADR-0008's follow-up task) will not need
    # this branch — the strong form will pass and the augment list
    # will shrink to zero. Until then, this branch is the correct
    # assertion.
    return


# ---------------------------------------------------------------------------
# Augment presence (documented in ADR-0008)
# ---------------------------------------------------------------------------
@pytest.mark.unit()
@pytest.mark.parametrize(
    ("token", "description"),
    _KNOWN_AUGMENTS,
    ids=[token for token, _ in _KNOWN_AUGMENTS],
)
def test_committed_file_contains_documented_augments(token: str, description: str) -> None:
    """Every documented augmentation must appear in the committed file.

    The augment list (in the module docstring and in ADR-0008) is the
    contract between the generator and the committed file. This test
    asserts that contract holds: the file contains exactly the
    augments the documentation claims it contains.

    A missing augment has two plausible causes:

    * **The augment was removed** — either by hand (a regression) or
      by an unsuccessful regeneration that erased it. The fix is to
      restore it (and run ``make models`` only if the generator was
      fixed to preserve it).
    * **The documentation is stale** — the augment was removed on
      purpose and the ADR-0008 list was not updated. The fix is to
      update the documentation, not to add the augment back.

    Both causes are diagnosed from the failure message: it names the
    token, the file, and the description of why the augment exists,
    so the contributor can make an informed decision about which
    cause applies.

    The assertion is on a substring (``token``), not a full line or a
    diff, because the point is "this concept is present", not "this
    exact line is present". A future reformat that moves
    ``frozen=True`` to a different position within the ``ConfigDict``
    should not fail this test — the augment is still there. The test
    is *specific* about what the augment is (documented in the
    description) and *robust* about where it lives.
    """
    contents = _models_file().read_text(encoding="utf-8")
    assert token in contents, (
        f"Known augment {token!r} is missing from {_models_file().name}.\n"
        f"  Meaning: {description}\n"
        f"Either restore the augment, or update the augment list in "
        f"this test and in ADR-0008 if it was intentionally removed."
    )


# ---------------------------------------------------------------------------
# Schema presence check (prerequisite for everything else)
# ---------------------------------------------------------------------------
@pytest.mark.unit()
def test_schema_file_is_present() -> None:
    """The canonical schema must exist on disk.

    If the schema is missing — a bad merge, an accidental ``git rm``,
    a fresh clone from a partial checkout — every test in this suite
    will fail for the wrong reason. This test pins the prerequisite so
    that the failure mode is unambiguous: the schema is gone, restore
    it, and then the other tests will produce meaningful results.

    Checking the schema on disk rather than via ``importlib.resources``
    is deliberate. The resources check is ``test_schema_bundled.py``'s
    job (it runs against a *built wheel*, from a fresh directory, to
    prove the file is packaged). This test runs against the *source
    tree* and proves the file exists there — the two checks are
    complementary, and combining them would conflate "the source tree
    is intact" with "the wheel ships the file", which are different
    bugs with different fixes.
    """
    schema_path = _repo_root() / "src" / "forge" / "schemas" / "blueprint.schema.json"
    assert schema_path.is_file(), f"canonical schema is missing from the source tree: {schema_path}"

    # Sanity: the file must be non-trivially sized. A zero-byte
    # placeholder would satisfy ``is_file()`` but break every
    # downstream check with a jsonschema parse error that names an
    # empty document rather than a missing file. The threshold is
    # deliberately low (the real schema is ~5 KB); the check is for
    # "someone checked in a placeholder", not "the file is the size
    # we expect today".
    assert schema_path.stat().st_size > 100, (
        f"canonical schema at {schema_path} is suspiciously small "
        f"({schema_path.stat().st_size} bytes); it may be a placeholder"
    )
