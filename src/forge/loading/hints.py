"""Deterministic path → hint catalogue.

Why a catalogue (and not inline strings at each call site)
----------------------------------------------------------
Hints are user-facing text that must be:

    * **Consistent** — "use full SemVer" must say the same thing whether
      the error came from the schema phase, the model phase, or a future
      compatibility phase. Inline strings drift within a release.

    * **Testable** — the acceptance criteria promise *every* error carries
      a hint. That promise is only verifiable if hints are queryable by
      path, without constructing a whole loader pipeline.

    * **Extensible** — when a new schema field is added, adding its hint
      should be a one-line edit in one file, not a scavenger hunt across
      the codebase.

    * **Reviewable** — hint wording is user experience. Keeping every
      hint in one file makes UX review a diff of a single document
      instead of a diff across dozens of modules.

The catalogue is a list of (predicate, hint) pairs, evaluated top-to-
bottom with first match winning. That ordering is deliberate: the most
specific patterns come first, so `verification.0.command` matches the
"checks with type 'command' must include 'command'" hint before the
generic "verification field" fallback. Contributors adding a rule must
place it above the patterns it is more specific than, and the test suite
enforces that every documented path still gets a non-empty hint.

Why predicates, not a dict or a regex table
-------------------------------------------
A dict would require every possible path to be enumerated — impossible
once variable and file indices are involved (`variables.0.type`,
`variables.1.type`, ...). A regex table would work but reads poorly for
reviewers who are not regex-fluent, and every rule would need to be
compiled. Small lambdas over the normalised dotted string are the sweet
spot: readable as English, easy to test, easy to add to.

The predicates take the *normalised* path (dots only, no leading slash)
so rules never have to remember the JSON-Pointer dialect. Normalisation
happens once, in `hint_for()`.

Phase-prefix tolerance
----------------------
Every error the loader emits has its path prefixed with the phase that
produced it — ``schema.version``, ``model.category``, and so on (see
``loading/aggregator.py::from_phase``). The catalogue's predicates
therefore match the *field* portion of the path without caring about
the prefix. Two patterns are used, deliberately:

* **Top-level identity fields** (``version``, ``category``, ``name``,
  ``description``, ``maintainer``, ``schema_version``) use
  ``p == "<field>" or p.endswith(".<field>")``. The first clause
  handles the unprefixed form a test or a future non-aggregator
  caller might pass; the second handles every prefixed form the
  aggregator produces (``schema.version``, ``model.version``, and
  any future phase). A rule that used only ``p == "<field>"``
  would silently fall through to the default for every production
  error — a bug that was live in this catalogue until the phase-
  tolerance tests caught it.

* **The top-level ``id``** is the exception: it deliberately uses
  ``p == "id" or p.endswith("schema.id") or p.endswith("model.id")``
  rather than ``p == "id" or p.endswith(".id")``. The reason is
  that ``endswith(".id")`` would *also* match ``variables.0.id`` and
  ``verification.0.id``, which need different hints (snake_case
  entry ids) and are handled by later rules. The current form
  matches only the field that appears at the document root,
  whether prefixed or not.

* **Nested fields** (``verification.*``, ``variables.*``,
  ``files.*``, ``conditional_files.*``) use substring matching
  (``"variables" in p``) combined with a suffix check
  (``p.endswith("type")``). Substring matching is naturally
  prefix-tolerant: a path of ``schema.variables.0.type`` still
  contains ``"variables"`` and still ends with ``"type"``, so the
  rule fires identically whether the path arrived prefixed or not.

Why a module-level default
--------------------------
Every hint catalogue will eventually be asked about a path it does not
know — a new schema field whose hint has not been added yet, a typo in
a caller, an error from a future phase. The two unacceptable outcomes
are (a) return an empty string and (b) raise. Both would break the "every
error carries a hint" invariant at exactly the moment the user is already
having a bad day (their file is invalid). A safe, generic default that
points at the reference document is better than either failure mode:

    * The user is not stuck — they are pointed at the schema reference,
      which is authoritative.
    * The maintainer is not stuck — an empty-hint assertion in tests
      catches the missing catalogue entry before it ships; the fallback
      exists to keep production usable during the window between a
      schema change and its hint being added.
    * The system is not stuck — a missing hint is never a crash.

The default is intentionally short so it reads as a pointer, not as
advice: "See docs/reference/SCHEMA_REFERENCE.md for the field's allowed
values." A longer default would start to feel authoritative and users
would stop looking for a specific hint.

What is deliberately NOT in this module
---------------------------------------
* **No filesystem access.** The catalogue is pure data; it must be
  importable from the loader without dragging in `pathlib` or any I/O.
* **No phase names.** Whether an error came from the schema or the model
  is not the hint's business — the path is what the user edits. If a
  caller prefixes a path with `schema.` or `model.`, the predicates below
  are written to tolerate it. This is why top-level rules use
  `p == "<field>" or p.endswith(".<field>")` rather than just `==`.
* **No i18n scaffolding.** The project ships in English today; adding
  a translation layer speculatively would freeze an API for a
  requirement that does not exist. If i18n becomes real, `hint_for`
  gains a `locale` parameter and the catalogue becomes a per-locale
  dict — a change confined to this file.
* **No references to internal identifiers.** The default points at a
  public document path (`docs/reference/SCHEMA_REFERENCE.md`), never at
  a WBS ID, a Jira ticket, or an internal class name. Hints are read by
  users, not by maintainers.

Ordering
--------
First match wins. The order below is:

    1. Root-level fields (schema_version, id, version, ...) — unique
       paths, no ambiguity.
    2. `verification.*` — before `variables.*` because verification
       errors block scaffolding entirely, not just part of the output.
    3. `variables.*` — variable errors are the most common user mistake
       in practice (wrong `type`, missing `validation.enum`).
    4. `files.*` and `conditional_files.*` — path errors, less common.
    5. `template.*` — the filesystem phase, which emits paths like
       `files.0.source` but whose message references a missing template;
       this rule catches the residual "template" cases directly.
    6. The default.

If a new rule needs to be more specific than an existing one, it must be
placed above it. The test suite (`tests/unit/loading/test_hints.py`) has
a table of representative paths with expected hint substrings, which
will fail loudly if a new rule accidentally shadows an existing one.

Testing strategy
----------------
`test_hints.py` asserts seven properties:

    * **Totality** — every path returns a non-empty string; nothing
      raises, including ``None`` (accepted gracefully — see
      ``hint_for``'s docstring).
    * **Specificity** — known paths get their specific hint, not the
      default.
    * **Determinism** — calling twice returns the same result.
    * **Ordering** — specific rules precede general ones.
    * **Phase tolerance** — ``hint_for("version")`` equals
      ``hint_for("schema.version")`` and ``hint_for("model.version")``,
      and similarly for every other top-level field.
    * **Fallback** — unknown paths get the documented default, and the
      default does not leak internal identifiers.
    * **Fault isolation** — a predicate that raises is logged and the
      catalogue falls through to the next rule (or to the default),
      rather than propagating the exception. This is the property that
      makes the ``try``/``except``/``continue`` block in ``hint_for``
      defensible: the exception is not silently swallowed, it is
      surfaced in the log, and the caller still gets a usable hint.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

# Module-level logger. The catalogue emits ``WARNING`` records when a
# predicate raises, so an operator troubleshooting a hint-quality
# complaint has a breadcrumb to follow. ``__name__`` resolves to
# ``forge.loading.hints`` under normal import, which means a future
# logging configuration can target this module specifically without
# filtering through every other module's records.
_logger = logging.getLogger(__name__)


# Type alias for readability. A rule matcher takes the normalised dotted
# path and returns True if the rule applies. Predicates are pure functions
# of the path — no captures over loader state, no reference to `phase`,
# no reference to `source`.
Rule = Callable[[str], bool]


# ---------------------------------------------------------------------------
# The catalogue.
#
# Each entry is (matcher, hint). The matcher runs against the normalised
# path (dots only, no leading slash, no phase prefix *stripped* — the
# phase prefix may be present and rules must tolerate it). First match
# wins.
#
# Style guide for hints (enforced by review, not by code):
#   * Imperative: "Use ...", "Provide ...", "Set ...", "Copy ...".
#   * Concrete: name a valid example when the field has a constrained
#     shape (`'python.fastapi.api'`, `'1.0.0'`).
#   * Short: one sentence. Users read hints in a terminal; wrapping is
#     a sign the hint is doing too much.
#   * No back-references to the message: the hint adds information the
#     message did not already convey. "Fix the version" is not a hint;
#     "Use full SemVer, e.g. '1.0.0' (not '1.0')" is.
#   * No internal jargon: "SemVer", "JSON Pointer", "UTF-8" are fine
#     (developers use them); "phase 2", "aggregator", "loader" are not.
# ---------------------------------------------------------------------------

_RULES: list[tuple[Rule, str]] = [
    # -- Root-level identity fields ----------------------------------------
    #
    # Each rule matches its field whether prefixed (``schema.version``,
    # ``model.version``) or not (``version``). The aggregator always
    # prefixes, so the ``p == "<field>"`` clause is a safety net for
    # direct callers (tests, future non-aggregator paths) rather than
    # the primary path.
    (
        lambda p: p == "schema_version" or p.endswith(".schema_version"),
        "Set schema_version to '1.0.0' — the schema version this CLI "
        "understands. Do not invent a future version.",
    ),
    (
        # ``id`` does NOT use ``endswith(".id")`` because that would
        # shadow the nested ``variables.0.id`` and ``verification.0.id``
        # rules below, which need a different hint ("snake_case entry
        # id") than the top-level id ("dotted lowercase identifier").
        # The top-level id is the only one that can appear as
        # ``id``, ``schema.id``, or ``model.id``.
        lambda p: p == "id" or p.endswith("schema.id") or p.endswith("model.id"),
        "Provide a dotted lowercase identifier such as "
        "'python.fastapi.api'. Every segment must start with a letter and "
        "contain only lowercase letters, digits, and dots.",
    ),
    (
        lambda p: p == "name" or p.endswith(".name"),
        "Provide a short human-readable name (1 to 120 characters). "
        "This is what users see in `forge list`.",
    ),
    (
        # ``min_forge_version`` is the compatibility-block sibling of
        # the top-level ``version`` and shares its SemVer rule. Both
        # must be matched before a hypothetical generic rule for
        # ``compatibility.*`` (there is none today) can shadow them.
        lambda p: p == "version" or p.endswith(".version") or p.endswith(".min_forge_version"),
        "Use full SemVer: MAJOR.MINOR.PATCH, e.g. '1.0.0' — not '1.0' " "and not 'v1.0.0'.",
    ),
    (
        lambda p: p == "description" or p.endswith(".description"),
        "Add a one-paragraph description (1 to 2000 characters) explaining "
        "what the blueprint generates.",
    ),
    (
        lambda p: p == "category" or p.endswith(".category"),
        "Pick one of: api, automation, frontend, library, cli, other.",
    ),
    (
        lambda p: p == "maintainer" or p.endswith(".maintainer"),
        "Provide a handle or email (≤ 200 characters), or omit the field "
        "entirely — it is optional.",
    ),
    # -- Verification contract ---------------------------------------------
    #
    # Verification errors are placed before variables/files because they
    # block scaffolding entirely (a broken check means the generated
    # project cannot be verified), while variable and file errors block
    # only parts of the output.
    (
        lambda p: "verification" in p and p.endswith("id"),
        "Give each verification check a unique snake_case id, e.g. "
        "'tests_pass' or 'has_pyproject'.",
    ),
    (
        lambda p: "verification" in p and p.endswith("type"),
        "Check type must be either 'command' (run a command) or "
        "'required_file' (assert a file exists).",
    ),
    (
        lambda p: "verification" in p and p.endswith("command"),
        "Checks with type 'command' must include the 'command' field — "
        "the shell command to run. Example: 'pytest -q'.",
    ),
    (
        lambda p: "verification" in p and p.endswith("path"),
        "Checks with type 'required_file' must include the 'path' field "
        "— the file whose existence is asserted, relative to the "
        "generated project root.",
    ),
    (
        lambda p: "verification" in p and p.endswith("timeout"),
        "Timeout is in seconds and must be between 1 and 3600. The "
        "default is 60 seconds if omitted.",
    ),
    # -- Variables ---------------------------------------------------------
    (
        lambda p: "variables" in p and p.endswith("id"),
        "Give each variable a unique snake_case id, e.g. 'project_name' " "or 'include_docker'.",
    ),
    (
        lambda p: "variables" in p and p.endswith("type"),
        "Variable type must be one of: string, boolean, enum.",
    ),
    (
        lambda p: "variables" in p and p.endswith("description"),
        "Add a short description (≤ 500 characters) explaining what the "
        "variable controls — this is shown in the interactive wizard.",
    ),
    (
        lambda p: "variables" in p and p.endswith("validation.enum"),
        "Enum variables need at least one allowed value under "
        "validation.enum, e.g. validation: { enum: [fast, slow] }.",
    ),
    (
        lambda p: "variables" in p and p.endswith("validation.pattern"),
        "Provide a regular expression the value must match in full. "
        "Escape backslashes: '^[a-z]+$' in YAML is written '^[a-z]+$'.",
    ),
    (
        lambda p: "variables" in p and p.endswith("default"),
        "The default's type must match the variable's declared type: "
        "strings use quotes, booleans use true/false.",
    ),
    # -- File mappings -----------------------------------------------------
    (
        lambda p: ("files" in p or "conditional_files" in p) and p.endswith("source"),
        "Reference a template file that exists inside the blueprint "
        "directory. Paths are relative to the blueprint's own folder.",
    ),
    (
        lambda p: ("files" in p or "conditional_files" in p) and p.endswith("destination"),
        "Provide a destination path inside the generated project. "
        "{{variable}} interpolation is allowed, e.g. 'src/{{project_name}}/'.",
    ),
    (
        lambda p: "conditional_files" in p and p.endswith("condition"),
        "Write a simple boolean expression over variable ids, e.g. "
        "'include_docker == true'. Supported operators: ==, !=, and, or, not.",
    ),
    # -- Filesystem phase (template existence) -----------------------------
    #
    # The filesystem phase emits paths like `files.0.source` (caught
    # above) or, for template-path traversal issues, paths containing
    # 'template'. This rule is the catch-all for that residual class.
    (
        lambda p: "template" in p.lower(),
        "Copy or rename the missing template file inside the blueprint "
        "directory, or remove the reference from the blueprint.",
    ),
]


# ---------------------------------------------------------------------------
# The default hint.
#
# Applied when no rule matches. Kept short so it reads as a pointer, not
# as advice; the pointer target is a public document, not an internal
# identifier. Never empty — that is the invariant this module exists to
# guarantee.
# ---------------------------------------------------------------------------
_DEFAULT_HINT = (
    "See docs/reference/SCHEMA_REFERENCE.md for this field's allowed " "values and examples."
)


def hint_for(path: str | None) -> str:
    """Return a deterministic, non-empty hint for a dotted path.

    Parameters
    ----------
    path
        A dotted field path such as `version`, `variables.0.type`, or
        `schema.version`. JSON Pointer form (`/variables/0/type`) is
        accepted and normalised before matching. An empty path *or*
        ``None`` means "no specific path is available" (used by parse
        errors and root-level non-mapping errors) and matches no rule,
        receiving the default.

        The parameter accepts ``None`` (rather than raising on it)
        because the catalogue is called *while rendering a failure*.
        A caller bug that passes ``None`` must degrade to a usable
        hint, not to a traceback that hides the original error. The
        type is annotated ``str | None`` so type-checkers know this is
        an accepted value, not an oversight.

    Returns
    -------
    str
        A non-empty, imperative, user-facing hint. The same path always
        yields the same hint within a process (deterministic).

    Notes
    -----
    This function is pure: no I/O, no globals mutated, no caching. Any
    future caching (e.g. an LRU for hot paths) must preserve determinism
    — a cache keyed on the normalised path satisfies this trivially.

    Normalisation collapses JSON Pointers to dots, but does *not*
    collapse leading/trailing dots or collapse repeated dots. Callers
    passing malformed paths still get a hint (the default), which is the
    right failure mode: a path rendering issue must never become an
    "empty hint" issue.

    Fault isolation: a predicate that raises is caught and logged, and
    the catalogue falls through to the next rule. This means a bug in
    one rule degrades the hint quality for the paths that rule would
    have matched — but it never breaks error rendering, and the log
    record gives a maintainer the breadcrumb they need to find the bug.
    The alternative (propagate the exception) would turn a hint-lookup
    problem into a rendering-time crash, at exactly the moment the user
    is already seeing a failure.
    """
    if not path:
        # Covers both ``""`` (root) and ``None`` (caller bug). Both get
        # the default. The branch is documented at the top of the
        # function's Notes section; keeping it in one place means a
        # future contributor who wants stricter behaviour has a single
        # location to change and a single place to update the
        # corresponding test.
        return _DEFAULT_HINT

    normalised = path.strip("/").replace("/", ".")

    for index, (matcher, hint) in enumerate(_RULES):
        try:
            if matcher(normalised):
                return hint
        except Exception as exc:
            # A misbehaving predicate (a future rule with a bug, a
            # predicate that assumes a shape the path does not have,
            # etc.) must never break error rendering: this function
            # runs *while reporting a failure*, and a traceback here
            # would hide the original error — the user would see a
            # Python stack instead of "your version field is wrong".
            #
            # We log the exception at WARNING (not ERROR, because the
            # function still produced a hint; not DEBUG, because a
            # broken predicate is a maintainer problem worth noticing
            # in production logs). ``exc_info=True`` attaches the
            # traceback, which names the line inside the predicate —
            # the actual diagnostic a future maintainer needs.
            #
            # The index is included so the log line names *which* rule
            # failed. A reader can count entries in ``_RULES`` to find
            # the offender without re-running the program; that is
            # faster than reading every predicate. The exception class
            # name is included as a compact grep target for bug-class
            # trends ("did the same rule raise twice, or is this a new
            # bug?").
            #
            # ``bandit`` flags this block with S112 ("try-except-
            # continue detected, consider logging the exception"). This
            # log call is precisely the fix that S112 requests. The
            # ``continue`` is retained because the design intent
            # requires it — see the paragraph above. ``bandit`` wants
            # the swallowing to be observable, not absent; the log
            # call provides that.
            _logger.warning(
                "hint predicate at index %d raised %s; falling through",
                index,
                type(exc).__name__,
                exc_info=True,
            )
            continue

    return _DEFAULT_HINT
