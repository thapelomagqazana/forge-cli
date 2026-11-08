"""Unit tests for the path → hint catalogue in `loading/hints.py`.

What this suite must prove
--------------------------
The hint catalogue is the single source of every actionable fix
suggestion the loader produces. Every `ValidationError` gets its
`hint` field from `hint_for(path)`, so the catalogue has three
contracts:

1. **Totality.** Every path a caller could ask about returns a
   non-empty string. There is no path — not `""`, not `"unknown.thing"`,
   not a path with a trailing slash — for which the catalogue returns
   `None`, `""`, or raises.

2. **Specificity.** Known paths get their *specific* hint, not the
   default. `hint_for("version")` must return the SemVer hint, not
   "see the docs." This is what makes a hint useful: a generic
   pointer at a schema reference is what the default is for; every
   other path should do better.

3. **Determinism.** The same path returns the same hint on every call,
   in every process. A future "improvement" that injected randomness
   (e.g. rotating hints to show variety, or picking among several
   plausible wordings) would break user scripts that parse
   ``forge validate`` output. Determinism is a stable contract, not
   an accident of the current implementation.

Two subtle contracts that this suite also pins:

4. **First-match-wins ordering.** The catalogue is an ordered list of
   ``(predicate, hint)`` rules, and the first matching rule's hint
   is returned. This ordering is load-bearing: a rule for
   ``verification.0.command`` must precede a generic rule for
   ``verification.*``, or the specific hint never fires. The
   ``TestRuleOrdering`` class pins the ordering of the most
   order-sensitive pairs, so a future contributor who appends a
   rule in the wrong place (or reorders the list "for readability")
   fails a test before merging.

5. **Phase-prefix tolerance.** The catalogue is used from the loader
   *after* the aggregator has prefixed paths with ``schema.`` or
   ``model.``. The catalogue's predicates are therefore written to
   match on the path's *shape* (``"variables" in p``, ``p.endswith(".type")``)
   rather than on its prefix. A future contributor who "tightens"
   a predicate to require a specific prefix would silently break
   hints for the other phase's errors — and no test would catch it
   unless this suite asserts that prefixed paths also match.

What this suite is *not* trying to prove
----------------------------------------
* **Exact hint text.** Hint wording is a UX concern; the catalogue
  is designed so each rule's text can change without breaking a
  test. The tests assert that the returned hint *contains* specific
  keywords (``"SemVer"``, ``"enum"``, ``"copy"``), not that it
  equals a full sentence. A future copy editor who tightens "Use
  full SemVer" to "Use full semantic version" should not need to
  update a test.

* **Coverage of every path the schema produces.** The catalogue
  necessarily falls back to the default for paths it does not know
  (a new field added to the schema without a corresponding rule).
  This is a *desirable* fallback — the alternative is a raise,
  which would break error rendering in production. A test that
  asserted "every schema path has a specific hint" would couple
  the hint catalogue to the schema's current field set and would
  fail every time the schema grows, without proving the
  catalogue's behaviour is correct. The right place for that
  assertion is the schema's own test suite (which can add a
  new field and a test that the catalogue has a hint for it in the
  same commit).

* **Locale.** The catalogue is English-only today. A locale
  parameter would need to exist before it could be tested; this
  suite deliberately does not presuppose one.

Structure
---------
Tests are grouped by *contract* (totality, specificity, determinism,
ordering, tolerance, fallback), not by catalogue entry. A reader
looking for "how does the catalogue behave on an unknown path?"
finds the answer in ``TestFallback``; a reader looking for "which
ordering is guaranteed?" finds it in ``TestRuleOrdering``. This is
the same grouping strategy used elsewhere in the loading test suite
and for the same reason: a test name should answer a question a
reader could have, and a reader's question is about behaviour, not
about which specific rule fires.
"""

from __future__ import annotations

import pytest

from forge.loading.hints import hint_for


# =============================================================================
# Totality — every input produces a non-empty string
# =============================================================================
class TestTotality:
    """No input makes ``hint_for`` return empty, ``None``, or raise.

    Totality is the catalogue's most important property. A hint is
    rendered while the user is looking at a failure; if the
    catalogue raised, the failure would turn into a crash. If it
    returned ``""`` or ``None``, the user would see a broken-looking
    error ("hint: " with nothing after it) that provides no help.
    Either outcome would break the loader's KPI that "100% of errors
    carry a hint."

    The test cases are chosen to exercise every category of input the
    loader could pass:

    - an empty path (parse errors, non-mapping-root errors)
    - a root-level field the catalogue knows
    - a nested field the catalogue knows
    - a nested field with an array index
    - a path with a phase prefix (``schema.version``)
    - a path with a fragment-pointer prefix (``/variables/0/type``)
    - a path the catalogue has never seen
    """

    @pytest.mark.parametrize(
        "path",
        [
            "",  # root — parse errors
            "id",
            "version",
            "category",
            "variables.0.type",
            "variables.2.validation.enum",
            "verification.0.command",
            "conditional_files.0.condition",
            "files.0.source",
            "schema.version",  # phase-prefixed
            "model.version",  # phase-prefixed
            "/variables/0/type",  # JSON Pointer form
            "unknown.totally.unknown",  # not in the catalogue
            "a.b.c.d.e.f",  # deeply nested unknown
            "trailing.",  # malformed
            ".leading",  # malformed
            "//double//slash//",  # pathological
        ],
    )
    def test_returns_a_string(self, path: str):
        result = hint_for(path)
        assert isinstance(result, str), (
            f"hint_for({path!r}) returned {type(result).__name__}, " f"expected str"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "id",
            "version",
            "unknown.totally.unknown",
            "//double//slash//",
        ],
    )
    def test_returns_a_non_empty_string(self, path: str):
        assert hint_for(path).strip(), (
            f"hint_for({path!r}) returned an empty or whitespace-only "
            f"string; the catalogue must always return a usable hint"
        )

    def test_none_input_returns_default(self):
        """The catalogue accepts ``None`` and returns the default hint.

        Rationale: the catalogue runs while the loader is rendering a
        failure. A crash here would turn "your blueprint is wrong"
        into a Python traceback — a worse outcome than a generic
        hint. ``hint_for`` treats ``None`` and ``""`` the same way
        (both mean "no specific path"), so both get the default
        hint. This is documented in ``hint_for``'s signature
        (``path: str | None``) and docstring.

        The alternative design — raising ``TypeError`` on ``None`` —
        would be defensible in isolation, but it would push the
        failure into error-rendering territory, where a crash is
        the worst possible outcome. A caller who passes ``None``
        gets a usable hint, and a subsequent caller-side fix (raising
        ``TypeError`` *before* calling ``hint_for``) remains an
        option. If that design is ever adopted, this test should be
        replaced by one that asserts the raise — the point is not
        to pin "accepts" forever, it is to pin "the failure mode is
        safe and documented."
        """
        hint = hint_for(None)  # type: ignore[arg-type]
        assert isinstance(hint, str)
        assert hint.strip()
        # The same generic pointer an unknown path gets; not a
        # rule-specific hint, because ``None`` carries no path
        # information to match on.
        assert "docs" in hint.lower() or "reference" in hint.lower()


# =============================================================================
# Specificity — known paths get their specific hint, not the default
# =============================================================================
class TestSpecificity:
    """Known paths receive a hint that names the *specific* fix.

    "Non-empty" is the loader's KPI; "specific" is what makes the hint
    useful. A test that only asserted non-emptiness would pass a
    catalogue that returned "see the docs" for every path — a
    technically-non-empty regression that would make every error
    less actionable than before.

    The assertion pattern here is *keyword-in-hint*, not
    *hint-equals-string*: the catalogue's exact wording is UX text
    that will evolve, but the *keyword* the user needs to recognize
    ("SemVer", "enum", "copy", "mapping") is the stable part of the
    message. A future copy editor who rewrites "Use full SemVer" to
    "Provide a semantic version" would break the keyword test only if
    they dropped "semver" entirely, which is unlikely and easy to
    fix.
    """

    @pytest.mark.parametrize(
        ("path", "keyword"),
        [
            # Root-level identity fields — the keywords are chosen from
            # the domain vocabulary a user would naturally search for.
            ("schema_version", "1.0.0"),
            ("id", "dotted"),
            ("version", "semver"),
            ("category", "api"),
            ("description", "description"),
            ("name", "name"),
            # Verification checks.
            ("verification.0.type", "command"),
            ("verification.0.command", "command"),
            ("verification.0.path", "path"),
            ("verification.0.timeout", "seconds"),
            # Variables — the most common user mistake is a wrong type
            # or a missing enum payload.
            ("variables.0.id", "snake_case"),
            ("variables.0.type", "string"),
            ("variables.0.validation.enum", "enum"),
            ("variables.0.validation.pattern", "regular expression"),
            ("variables.0.description", "description"),
            # File mappings — the fix is usually "create the file" or
            # "check the path."
            ("files.0.source", "template"),
            ("files.0.destination", "destination"),
            ("conditional_files.0.condition", "expression"),
        ],
    )
    def test_known_path_has_specific_hint(self, path: str, keyword: str):
        hint = hint_for(path).lower()
        assert keyword in hint, (
            f"hint for {path!r} does not mention {keyword!r}; "
            f"got: {hint!r}. This means the path matched the wrong "
            f"rule (probably the fallback), and the user is being "
            f"given a generic pointer instead of a specific fix."
        )


# =============================================================================
# Determinism — same input, same output, every time
# =============================================================================
class TestDeterminism:
    """The catalogue is a pure function of the path.

    Determinism is a compatibility contract, not an implementation
    detail: user scripts parse ``forge validate`` output, and
    "sometimes the hint says X, sometimes Y for the same error" would
    break them. It also makes tests stable — a non-deterministic
    catalogue would make every loader test that asserts on hint
    presence flaky.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "version",
            "variables.0.type",
            "files.0.source",
            "",
            "unknown.totally.unknown",
        ],
    )
    def test_same_path_same_hint(self, path: str):
        first = hint_for(path)
        second = hint_for(path)
        third = hint_for(path)
        assert first == second == third

    def test_no_state_between_calls(self):
        """Calling one path must not affect the next.

        A caching bug (e.g. an ``lru_cache`` with a bad key function,
        or a module-level accumulator) would surface here: after
        asking about ``version`` and then about ``category``, asking
        about ``version`` again must produce the same hint as the
        first time.
        """
        before = hint_for("version")
        hint_for("category")
        hint_for("files.0.source")
        hint_for("unknown.anything")
        after = hint_for("version")
        assert before == after


# =============================================================================
# First-match-wins ordering
# =============================================================================
class TestRuleOrdering:
    """Ordering of the catalogue is load-bearing; specific rules precede
    general ones.

    This class pins the *outcome* of the ordering, not the ordering
    itself: it asserts that for a given path the returned hint is
    the specific one, not the general one. A future contributor who
    reorders the catalogue (for readability, or to group rules by
    phase) will change the outcome for at least one of these paths,
    and the test will fail. That failure is the signal to move the
    offending rule back up — the ordering is a property of the
    catalogue's *behaviour*, and the tests express that property
    without pinning the implementation detail of "rule number 4 is
    the variables-type rule."

    Three orderings are pinned here because they are the ones most
    likely to be disturbed by a well-intentioned cleanup:

    1. ``verification.*.command`` must match before a hypothetical
       generic ``verification`` rule (there is not one today, but
       the rule is written specifically enough that adding one
       below it would shadow the specific hint).

    2. ``variables.*.validation.enum`` must match before
       ``variables.*.type`` — a rule that checked ``"variables" in
       path and p.endswith(".type")`` would not match
       ``variables.0.validation.enum`` (its suffix is ``.enum``),
       but a rule that checked ``"variables" in path`` alone and
       ran first would shadow both. The current rules are specific
       enough that this does not happen; the test pins that they
       stay that way.

    3. ``verification.*.command`` must precede ``verification.*``,
       which is the same shape as (1) but with real rules today:
       the ``"verification" in p and p.endswith("command")`` rule
       must fire before any generic ``"verification" in p`` rule
       (there is none today, but this test is the guard for a
       future addition).
    """

    def test_variables_type_beats_any_generic_variables_rule(self):
        hint = hint_for("variables.0.type").lower()
        assert "string" in hint, (
            f"hint for 'variables.0.type' does not mention the type "
            f"enum; got: {hint!r}. If a generic 'variables' rule "
            f"was added above the specific one, it is shadowing it."
        )
        # The specific rule for variables.type names the three
        # allowed values. A generic rule would not.
        assert "boolean" in hint or "enum" in hint

    def test_verification_command_beats_any_generic_verification_rule(self):
        hint = hint_for("verification.0.command").lower()
        assert "pytest" in hint or "command" in hint, (
            f"hint for 'verification.0.command' does not name the " f"command field; got: {hint!r}"
        )

    def test_variables_enum_beats_generic_variables_rule(self):
        hint = hint_for("variables.0.validation.enum").lower()
        assert "enum" in hint, (
            f"hint for 'variables.0.validation.enum' does not name " f"enum; got: {hint!r}"
        )
        # The enum-specific hint mentions 'validation.enum', which
        # the generic variables hint would not.
        assert "validation" in hint or "enum" in hint

    def test_files_source_beats_generic_files_rule(self):
        hint = hint_for("files.0.source").lower()
        assert "template" in hint, (
            f"hint for 'files.0.source' does not mention the template " f"file; got: {hint!r}"
        )


# =============================================================================
# Phase-prefix tolerance
# =============================================================================
class TestPhaseTolerance:
    """The same path with a phase prefix gets the same hint.

    The catalogue is called by the loader's aggregator *after*
    prefixing paths with ``schema.`` or ``model.``. If the catalogue
    required paths to be unprefixed, the loader would have to strip
    the prefix before looking up the hint — a coupling that would
    leak the aggregator's encoding into the catalogue. Instead, the
    catalogue's predicates are written to match on path shape, not
    prefix, and this test pins that contract.

    A future contributor who "tightens" a predicate to require the
    exact path (``p == "version"``) without the phase-tolerant form
    (``p == "version" or p.endswith(".version")``) would break hints
    for prefixed paths and only this test would catch it.
    """

    @pytest.mark.parametrize(
        "unprefixed",
        [
            "version",
            "id",
            "category",
            "variables.0.type",
            "files.0.source",
        ],
    )
    def test_schema_prefix_preserves_hint(self, unprefixed: str):
        unprefixed_hint = hint_for(unprefixed)
        prefixed_hint = hint_for(f"schema.{unprefixed}")
        assert unprefixed_hint == prefixed_hint, (
            f"schema.{unprefixed} returned a different hint than "
            f"{unprefixed}. The catalogue's predicates must be "
            f"phase-tolerant (use endswith/in, not ==/startswith)."
        )

    @pytest.mark.parametrize(
        "unprefixed",
        [
            "version",
            "id",
            "category",
            "variables.0.type",
            "files.0.source",
        ],
    )
    def test_model_prefix_preserves_hint(self, unprefixed: str):
        unprefixed_hint = hint_for(unprefixed)
        prefixed_hint = hint_for(f"model.{unprefixed}")
        assert unprefixed_hint == prefixed_hint, (
            f"model.{unprefixed} returned a different hint than "
            f"{unprefixed}. The catalogue's predicates must be "
            f"phase-tolerant."
        )


# =============================================================================
# JSON Pointer normalisation
# =============================================================================
class TestJsonPointerNormalisation:
    """JSON Pointer paths are normalised to dotted paths before matching.

    The aggregator produces JSON Pointers in its ``schema_errors`` and
    ``model_errors`` phase functions (``/variables/0/type``), but the
    aggregator's ``from_phase`` function then converts them to dotted
    paths before the catalogue sees them. So in practice, the
    catalogue never receives a JSON Pointer. But ``hint_for`` is a
    public function — a future caller (an IDE integration, an API
    endpoint) could pass a Pointer directly, and the catalogue would
    be expected to handle it. This test pins that expectation so the
    normalisation is preserved even if the aggregator's behaviour
    changes.
    """

    @pytest.mark.parametrize(
        ("pointer", "dotted"),
        [
            ("/version", "version"),
            ("/variables/0/type", "variables.0.type"),
            ("/files/0/source", "files.0.source"),
        ],
    )
    def test_pointer_and_dotted_get_same_hint(self, pointer: str, dotted: str):
        assert hint_for(pointer) == hint_for(dotted), (
            f"hint_for({pointer!r}) != hint_for({dotted!r}); the "
            f"catalogue must normalise JSON Pointers to dotted paths "
            f"before matching."
        )


# =============================================================================
# Fallback — unknown paths get the documented default
# =============================================================================
class TestFallback:
    """Unknown paths get the documented default, not ``None``, not raise.

    The default hint points at ``docs/reference/SCHEMA_REFERENCE.md``,
    which is the canonical user-facing field reference. A future
    contributor who changed the default to point at an internal doc,
    a WBS ID, or a class name would be violating the "hints are read
    by users, not by maintainers" rule documented in the module
    docstring. This test pins the *class* of default (a public doc
    pointer), not the exact filename.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "unknown",
            "unknown.field",
            "a.b.c.d.e.f.g.h",
            "definitely-not-in-the-catalogue",
            "x" * 1000,  # pathological length
        ],
    )
    def test_unknown_path_returns_default(self, path: str):
        hint = hint_for(path)
        assert hint, f"unknown path {path!r} returned an empty hint"
        assert "docs" in hint.lower() or "reference" in hint.lower(), (
            f"unknown path {path!r} returned a hint that does not "
            f"point at a public reference document: {hint!r}"
        )

    def test_default_does_not_leak_internal_identifiers(self):
        """The default hint must not mention WBS IDs, task numbers, or
        internal class names.

        Hints are user-facing. A default that said "see Task 1.2.1"
        or "see ``hints.py::_RULES``" would send the user to an
        internal artifact they cannot read. This test asserts the
        default names a *document*, not a *ticket* or a *symbol*.
        """
        hint = hint_for("completely.unknown.path")
        # None of these substrings should appear in a user-facing hint.
        forbidden = ["wbs", "task ", "``", "hints.py", "schema.py"]
        for token in forbidden:
            assert token.lower() not in hint.lower(), (
                f"default hint leaks internal reference {token!r}: " f"{hint!r}"
            )


# =============================================================================
# Robustness — the catalogue does not crash on pathological input
# =============================================================================
class TestRobustness:
    """Pathological paths do not crash the renderer.

    The catalogue runs while the loader is already reporting a
    failure. If a misbehaving predicate, a regex-compile failure, or
    any other internal bug raised during hint lookup, the failure
    rendering itself would crash, and the user would see a Python
    traceback instead of "your version field is malformed." That is
    worse than the original error. The catalogue is written to be
    defensive against such failures (see ``hint_for``'s try/except
    around the predicate call), and this class pins that behaviour
    for the input shapes most likely to trigger a defensive path:
    Unicode, very long strings, and strings that could be interpreted
    as regex patterns if a future rule used ``re.search``.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "路径.字段",  # non-ASCII — should not break normalisation
            "🚀.🔥",  # emoji
            "\\",  # escape character
            "^$.*+?[]{}()|",  # regex metacharacters
            "a" * 10_000,  # very long — should not cause a stack error
            "\x00\x01\x02",  # control characters
        ],
    )
    def test_pathological_path_returns_string(self, path: str):
        """Every input returns a string without raising."""
        result = hint_for(path)
        assert isinstance(result, str)
        assert result  # non-empty
