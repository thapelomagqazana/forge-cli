"""Criterion 6: the filter allowlist is documented and stays in sync.

The guarantee
-------------
`filters.ALLOWED_FILTERS` is the *executable* contract: it determines
which filters a template may use. `docs/reference/TEMPLATE_FILTERS.md`
is the *human* contract: it tells blueprint authors what they can write
and what each filter does.

These two artefacts must agree. If a maintainer adds a filter to code
without documenting it, the docs lie to blueprint authors (who never
discover the new capability). If a maintainer documents a filter that
is not in code, blueprint authors write templates that fail at render
time. Either failure mode is exactly the kind of silent drift that
accumulates as technical debt over months.

This file is the **enforcement mechanism** for that agreement. It is
the machine that makes the docs and the code stay in sync, so a future
contributor cannot accidentally let them drift.

Why a doc-sync test, not just "add a filter and update the docs"
---------------------------------------------------------------
"Just remember to update the docs" is a promise, not a contract. Every
mature project has stories of documentation that fell behind the code
because nobody remembered. The test converts the promise into a
contract: the build fails if the two disagree, and the failure message
names exactly which filter was forgotten and in which direction.

The test is cheap (one markdown scan, one set comparison), fast
(milliseconds), and catches every drift regardless of who introduced
it — the maintainer who added a filter, the technical writer who
removed a row, the merge that resolved a conflict incorrectly. That is
the value of an enforcement mechanism over a workflow habit.

The three failure modes this file catches
-----------------------------------------
1. **Undocumented filter.** A new entry in ``ALLOWED_FILTERS`` with no
   corresponding row in ``TEMPLATE_FILTERS.md``. Blueprint authors
   will never discover the filter. **Test:**
   ``test_every_allowed_filter_is_documented``.

2. **Ghost filter.** A row in ``TEMPLATE_FILTERS.md`` naming a filter
   that is not in ``ALLOWED_FILTERS``. Blueprint authors who follow
   the docs write templates that fail. **Test:**
   ``test_every_documented_filter_is_allowed``.

3. **Stale doc structure.** The doc file is missing, is empty, or its
   structure has drifted enough that the row-extraction regex no
   longer matches anything (e.g. the tables were converted to bullet
   lists). The doc-sync test silently passes with an empty set, which
   is worse than failing. **Test:**
   ``test_documentation_file_exists_and_parses``.

The third failure mode is the subtle one. If the doc file were deleted,
a naive doc-sync test that scanned "whatever rows it finds" would
extract zero rows and compare zero rows against fourteen filters — and
fail, correctly, with "every allowed filter is documented" showing all
fourteen missing. But if the doc file were *rewritten* to use a
different structure (say, a bullet list instead of a table), the
extractor would find zero rows in the new format, and the test would
fail with the *same* message ("every allowed filter is documented"),
which is technically correct but misleading: the failure looks like
"the author forgot to document a filter" when it is actually "the
doc-sync test's parser is out of date." The third test names the
distinct failure so diagnosis points at the right fix.

Row format contract
-------------------
The doc uses markdown tables. Each filter is documented as a row of
the form::

    | `filter_name` | Description |
    |---------------|-------------|
    | `upper`       | Uppercase    |

The backtick-quoted filter name is what the extractor matches.

**Fenced code blocks are excluded before extraction.** This is
load-bearing: the doc demonstrates row format inside a ``` fence (see
the "Table row format" section of ``TEMPLATE_FILTERS.md``), and that
demo row is *not* a documented filter. Without fence-awareness the
extractor would treat the demo name as a ghost and fail the sync test,
even though the doc is doing exactly what it should be doing.

The extraction pipeline is therefore::

    read bytes  →  strip fenced code blocks  →  regex on remaining lines

Nothing else. No heading awareness, no prose filtering, no HTML
parsing. The narrow scope is what makes the test auditable: every step
is visible in fifteen lines of code, and any drift between doc and
code that the pipeline misses is a bug in the pipeline, not a hidden
heuristic.

If a future maintainer wants to change the doc format (e.g. to
reStructuredText, or to prose paragraphs per filter), they must update
both this test's pipeline and the doc in the same commit. That is
deliberate friction: the format is a contract, and changing it is a
decision, not a side effect.

What this file does not test
----------------------------
- **Filter behaviour.** That is ``test_filter_allowlist.py``'s job.
  This file is about the *set* of names, not what each filter does.
- **Doc prose quality.** Whether the descriptions are accurate,
  whether examples are correct, whether the doc reads well — none of
  that is machine-checkable in a way that respects the value of prose.
  The doc is reviewed by humans; this test only guards the mechanical
  invariant (the *set* of filters).
- **The reference implementation of a filter.** If a filter's *name* is
  in the doc and in the code, but the doc's description of what the
  filter does is wrong, this test will not catch it. That is a
  documentation-quality problem, not a doc-sync problem, and it needs
  a different kind of test (a behaviour test cross-checked against the
  doc's examples). Out of scope here.

The narrow scope is intentional. A test that tried to verify both
"the set matches" and "every description is correct" would be brittle
against rewordings and would fail for reasons unrelated to drift. This
file's job is one thing: **the two sets agree**. That is the invariant
that produces technical debt if it drifts, and that is the invariant
worth enforcing.
"""

from __future__ import annotations

import re
from pathlib import Path

from forge.rendering.filters import ALLOWED_FILTERS

# The doc file lives at the repo root, four levels up from this test.
# ``parents[3]`` walks: test_filter_docs.py → rendering/ → unit/ →
# tests/ → repo root. The extra level compared to ``_FIX = parents[2]``
# in other test files is because fixtures live under
# ``tests/fixtures/`` (two up from the test) while docs live under
# ``docs/`` (three up from the *repo root*, which is one more hop).
_REPO_ROOT = Path(__file__).parents[3]
_DOC_PATH = _REPO_ROOT / "docs" / "reference" / "TEMPLATE_FILTERS.md"


# ---------------------------------------------------------------------------
# Fence stripping
#
# The doc legitimately demonstrates row format inside a ``` fence (see
# the "Table row format" section). A bare regex over the whole file
# cannot tell a demo row from a real one, so we strip fenced blocks
# first. This mirrors what every markdown renderer does: a row inside a
# fence is *code*, not a table.
#
# Supported fence forms:
#   ```                — the common 3-backtick fence
#   ```markdown        — 3-backtick with an info string (ignored here)
#   ````               — 4-backtick fence, used to nest a 3-backtick block
#
# A fence is "closed" by a run of backticks at least as long as the
# opening run. This is the CommonMark rule and matches GitHub's
# rendering, so what the reader sees is what the extractor sees.
# ---------------------------------------------------------------------------
_FENCE_OPEN = re.compile(r"^(\s*)(`{3,})(?:\s|$)")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,})\s*$")


def _strip_fenced_blocks(text: str) -> str:
    """Return *text* with everything inside ``` … ``` fences removed.

    The removed content is replaced with an empty line (preserving line
    numbers for any future test that wants to report a line of a failure
    against the original file). Line *count* is preserved; line
    *content* inside a fence is not. That is the minimum needed for a
    reliable extractor and no more.

    Unterminated fences drop everything to EOF. That matches how a
    reader's markdown renderer treats the same input, so the extractor
    and the human see the same document. If a doc has an unterminated
    fence, the fix is to close the fence — not to make the extractor
    guess where the fence "should have" ended.
    """
    lines = text.splitlines()
    out: list[str] = []
    fence_len = 0

    for line in lines:
        if fence_len == 0:
            m = _FENCE_OPEN.match(line)
            if m:
                fence_len = len(m.group(2))
                out.append("")  # placeholder to preserve line count
                continue
            out.append(line)
            continue

        # Inside a fence: look for the closer.
        m = _FENCE_CLOSE.match(line)
        if m and len(m.group(1)) >= fence_len:
            fence_len = 0
            out.append("")  # placeholder to preserve line count
            continue

        # Content inside the fence — drop it, keep the line count.
        out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Row extraction
#
# Matches a markdown table row whose first cell is a backtick-quoted
# lowercase identifier. The description and example cells are ignored;
# only the name matters for set comparison.
#
# Character class `[a-z_]+` is intentionally narrow: it matches the
# naming convention in `filters.py` and *rejects* names that violate it.
# If a maintainer adds `myFilter` to `ALLOWED_FILTERS`, the extractor
# will not recognise a doc row that spells it the same way, and the
# doc-sync test will fail with "undocumented filter: myFilter." That is
# the correct behaviour — the naming inconsistency should surface, not
# be papered over.
# ---------------------------------------------------------------------------
_FILTER_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)


def _documented_filters() -> set[str]:
    """Extract documented filter names, ignoring fenced code blocks.

    Returns an empty set if the doc is missing, empty, or unparseable.
    That case is caught by ``test_documentation_file_exists_and_parses``
    before the set-comparison tests run, so the empty set here is a
    deliberate signal ("nothing to compare") rather than a silent
    failure. If a future caller uses this helper without the structure
    check, they get an empty set — cheap, obvious, and consistent with
    "no doc, no documented filters."
    """
    if not _DOC_PATH.exists():
        return set()
    text = _DOC_PATH.read_text(encoding="utf-8")
    body = _strip_fenced_blocks(text)
    return set(_FILTER_ROW.findall(body))


# ---------------------------------------------------------------------------
# Failure mode 3: the doc file exists and its structure is parseable.
# ---------------------------------------------------------------------------
# Runs first (source order) so a missing or restructured doc fails with
# a diagnostic that names the *structural* problem before the set-
# comparison tests fail with their less-specific messages.
def test_documentation_file_exists_and_parses() -> None:
    """The doc file exists and contains at least one non-fenced row.

    Structural precondition for the two doc-sync tests below. Ensures a
    missing file or a rewritten doc is diagnosed as a *structure*
    problem, not as a *filter set* problem.

    Failure modes caught:

    - ``TEMPLATE_FILTERS.md`` is deleted. Without this test, both
      set-comparison tests would fail with messages naming all
      fourteen undocumented filters, which is technically correct but
      obscures the real cause (the file is gone).
    - The doc is rewritten to use a format the extractor does not
      recognise (prose paragraphs, bullet lists, a different table
      syntax). The extractor returns an empty set, and the filter-set
      tests fail with the same misleading message. This test catches
      it first with a clear "no parsable rows" message.
    - The doc exists but is empty or truncated. Same as above.

    The assertion is ``at least one row``, not ``exactly fourteen``.
    The precise count is asserted by the set-comparison tests
    (implicitly, through equality). Asserting a specific count here
    would make adding a filter require updating *three* places (code,
    doc, this test) instead of two — the redundant friction this test
    is designed to avoid.

    The existence check is separate from the parse check because they
    have different fixes: a missing file needs to be restored from git
    history; an unparseable file needs its structure changed to match
    the extractor (or vice versa). Two distinct failure modes, two
    distinct fixes; the messages distinguish them.
    """
    assert _DOC_PATH.exists(), (
        f"documentation file is missing: {_DOC_PATH}\n"
        "The file is required by criterion 6 (the filter allowlist must "
        "be documented). Restore it from git history or recreate it "
        "with a row for every entry in forge.rendering.filters.ALLOWED_FILTERS."
    )

    documented = _documented_filters()
    assert documented, (
        f"documentation file exists but contains no parsable filter "
        f"rows: {_DOC_PATH}\n"
        "The doc-sync extractor looks for markdown table rows of the "
        "form '| `filter_name` | description |', outside of fenced code "
        "blocks. Either:\n"
        "  - the file was rewritten to use a different structure "
        "(update the extractor in this test to match), or\n"
        "  - the file is empty/truncated (restore from git history), or\n"
        "  - the rows were converted to prose (the extractor requires "
        "backtick-quoted names in table cells)."
    )


# ---------------------------------------------------------------------------
# Failure mode 1: every allowed filter is documented.
# ---------------------------------------------------------------------------
def test_every_allowed_filter_is_documented() -> None:
    """Every filter in ``ALLOWED_FILTERS`` has a row in the doc.

    This is the "code added, doc forgotten" failure mode. A maintainer
    adds a filter to ``filters.py``, the code works, but blueprint
    authors never discover it because the doc does not mention it.
    Silent capability addition — harmless in the short term, corrosive
    in the long term as the doc becomes an increasingly incomplete
    picture of what templates can do.

    The failure message names the *specific* missing filters, sorted
    for deterministic output (a set's iteration order is not
    deterministic, and a failure message that changes between runs is
    hard to act on). A maintainer reading the message knows exactly
    what to add to the doc.

    Why ``ALLOWED_FILTERS`` is the source of truth, not the doc:

    The filter allowlist is *executable*: it is what templates actually
    use at render time. The doc is *advisory*: it describes what the
    allowlist allows. If they disagree, the allowlist is correct (it
    is what runs), and the doc must be brought into alignment. The
    test's job is to catch the disagreement, not to pick a winner — but
    the *failure message* assumes the allowlist is right and asks for
    a doc update, because that is the common direction of drift (code
    changes precede doc changes).
    """
    documented = _documented_filters()
    missing = set(ALLOWED_FILTERS) - documented
    assert not missing, (
        f"the following filters are in ALLOWED_FILTERS but not "
        f"documented in {_DOC_PATH}:\n"
        f"  {sorted(missing)}\n"
        "Add a row for each under the appropriate category header in "
        "the doc. The row format is:\n"
        "    | `filter_name` | One-line description | `{{ example }}` → `output` |\n"
        "See the existing rows for examples."
    )


# ---------------------------------------------------------------------------
# Failure mode 2: every documented filter is in the allowlist.
# ---------------------------------------------------------------------------
def test_every_documented_filter_is_allowed() -> None:
    """Every filter documented in the doc is in ``ALLOWED_FILTERS``.

    This is the "doc updated, code forgotten" failure mode — or, more
    commonly, "filter was removed from code but its doc row was not
    removed." A blueprint author reads the doc, uses the filter, and
    the template fails at render time with ``syntax.error``. Confusing
    for the user, embarrassing for the maintainer, and preventable
    with this test.

    The failure message names the *specific* ghost filters, sorted for
    deterministic output. Two plausible fixes are offered in the
    message:

    - Remove the doc row (if the filter was intentionally removed).
    - Restore the filter to ``ALLOWED_FILTERS`` (if the removal was
      accidental).

    Which fix is correct depends on *why* the drift happened, and
    that context lives outside the test. The message names both
    possibilities so the maintainer can choose, rather than the test
    guessing.

    Note on false positives: a row inside a ``` fence is *not*
    extracted (see ``_strip_fenced_blocks``), so a doc that
    demonstrates row format does not trigger this test. If a ghost
    appears here, it is a real ghost — the extractor has already
    filtered out demo content.
    """
    documented = _documented_filters()
    ghosts = documented - set(ALLOWED_FILTERS)
    assert not ghosts, (
        f"the following filters are documented in {_DOC_PATH} but not "
        f"present in ALLOWED_FILTERS:\n"
        f"  {sorted(ghosts)}\n"
        "Either:\n"
        "  - remove the doc row (the filter was intentionally removed "
        "from code), or\n"
        "  - restore the filter to filters.ALLOWED_FILTERS (the removal "
        "was accidental, and blueprint authors following the doc would "
        "have working templates before the removal)."
    )


# ---------------------------------------------------------------------------
# Cross-cutting: the sets are exactly equal (both directions at once).
# ---------------------------------------------------------------------------
def test_filter_sets_are_exactly_equal() -> None:
    """The documented set and the allowed set are identical.

    This test is *technically redundant* with the two tests above —
    two sets are equal if and only if neither is a strict superset of
    the other, so passing ``test_every_allowed_filter_is_documented``
    and ``test_every_documented_filter_is_allowed`` implies this test
    passes.

    The redundancy is deliberate, for two reasons:

    1. **Diagnostic clarity.** The two directional tests produce
       messages that name *which* direction drifted ("undocumented" vs.
       "ghost"). This test's failure message names both at once, which
       is a useful summary when a maintainer sees a full test run in
       CI and wants a one-line overview before drilling into the
       specific directional failures.

    2. **Cross-checking the directional tests.** If a future refactor
       of ``_documented_filters`` introduced a bug that made it return
       *every possible filter* (e.g. a regex so permissive it matched
       prose), ``test_every_documented_filter_is_allowed`` would fail
       with hundreds of ghosts — but the *other* directional test
       would pass (all allowed filters are still "documented"). This
       test asserts the sets are *equal*, not just that one contains
       the other, which catches the class of bug where the extractor
       is too permissive.

    The two directional tests are the primary failure-detectors; this
    test is the backstop. In a well-functioning test suite, both
    directional tests and this test pass or fail together; the value of
    having all three is that the failure messages cover both the
    specific diagnosis and the summary, without needing to run the
    suite twice.
    """
    documented = _documented_filters()
    allowed = set(ALLOWED_FILTERS)
    assert documented == allowed, (
        f"the documented and allowed filter sets disagree.\n"
        f"Only in ALLOWED_FILTERS (undocumented): {sorted(allowed - documented)}\n"
        f"Only in {_DOC_PATH.name} (ghosts):      {sorted(documented - allowed)}\n"
        "See the directional tests above for the specific fix for each "
        "direction."
    )


# ---------------------------------------------------------------------------
# Sanity: the extractor's format assumptions are documented as a test.
# ---------------------------------------------------------------------------
def test_doc_row_format_is_what_the_extractor_expects() -> None:
    """The doc uses the markdown table row format the extractor matches.

    This test is a *documentation* assertion about the extractor's
    assumptions, encoded as an executable check. The extractor's regex
    only matches ``| `name` | ... |`` rows outside fenced code blocks.
    If a future doc edit used a different formatting (say,
    ``| name | description |`` without backticks, or a bullet list),
    the extractor would silently miss the new format and the
    set-comparison tests would fail with a confusing message about
    filters being "undocumented" when they are actually documented in a
    different style.

    The test checks that at least one backtick-quoted row exists in the
    *stripped* body — i.e., in real documentation, not in a demo
    fence. That is a weaker check than the set equality above (which
    requires every filter to be present and correctly formatted), but
    it *names* the formatting assumption explicitly. If a maintainer
    sees this test fail while the set equality tests pass, they know
    the extractor is not finding any rows that the file contains — a
    formatting bug rather than a content bug.

    Why this is separate from
    ``test_documentation_file_exists_and_parses``:

    That test verifies the doc *exists* and produces *some* extractable
    rows. This test verifies the *format* the extractor assumes is
    actually used in the file. The distinction matters because a
    maintainer reading the test file should see the format constraint
    stated explicitly, not have to reverse-engineer it from the regex.
    Documentation of the constraint belongs in a test, not just in a
    comment next to the regex.
    """
    assert _DOC_PATH.exists(), (
        f"documentation file is missing: {_DOC_PATH} " "(cannot check row format)"
    )

    text = _DOC_PATH.read_text(encoding="utf-8")
    body = _strip_fenced_blocks(text)

    # The extractor requires backtick-quoted names inside table rows.
    # Checking the *stripped* body (not the raw file) is important: the
    # raw file's demo row inside a fence would satisfy this check while
    # the real rows were all missing, which would defeat the purpose.
    assert "| `" in body, (
        f"{_DOC_PATH} does not contain any markdown table rows of the "
        "form '| `filter_name` | ... |' outside of fenced code blocks.\n"
        "The doc-sync extractor requires this specific format: "
        "backtick-quoted filter names inside table cells, in real (not "
        "fenced) content. If you are rewriting the doc, either match "
        "this format or update the extractor in test_filter_docs.py to "
        "match the new format. Do not change one without the other."
    )


# ---------------------------------------------------------------------------
# Regression guard: fenced demo rows are ignored by the extractor.
# ---------------------------------------------------------------------------
def test_fenced_example_rows_are_ignored() -> None:
    """A row inside a ``` fence is a demonstration, not a filter.

    The "Table row format" section of ``TEMPLATE_FILTERS.md`` shows an
    example row inside a fenced code block so that readers can see the
    exact format. That example is not a documented filter, and the
    extractor must not count it as one — otherwise every doc revision
    that demonstrates row format would fail the doc-sync test with a
    misleading "ghost filter" message.

    This test is the regression guard for that behaviour. It is written
    against the *extractor's output* rather than the doc's content, so
    it stays valid regardless of what the doc chooses to demonstrate
    inside its fences: the invariant is "fenced content never
    contributes to the extracted set," not "the doc contains this
    specific demo row."

    On the specific incident that motivated this test: the doc
    previously used ``some_filter`` as its demo name, and a
    fence-unaware extractor counted it as a ghost. The fix was to make
    the extractor fence-aware. This test ensures the fence-awareness
    stays in place — if a future refactor of ``_strip_fenced_blocks``
    or ``_documented_filters`` silently drops the stripping step, this
    test fails with a message that names the incident, not with the
    indirect "ghost filter" failure from the directional tests.
    """
    documented = _documented_filters()

    # Any name that appears *only* inside a fence in the doc must not
    # appear in the extracted set. The current doc uses `example_name`
    # inside its "Table row format" fence; that name must not leak.
    #
    # Rather than hardcoding the demo name (which the doc may rename),
    # we assert the *stronger* invariant: every extracted name is a
    # real filter. This holds regardless of what the doc chooses to
    # demonstrate, and it catches the class of bug rather than the
    # specific instance.
    assert documented <= set(ALLOWED_FILTERS), (
        "the extractor produced names that are not in ALLOWED_FILTERS, "
        "which means it is reading fenced demo content as real "
        f"documentation. Ghosts: {sorted(documented - set(ALLOWED_FILTERS))}. "
        "Check _strip_fenced_blocks() in this file — it must strip "
        "every fenced block before the row regex runs."
    )
