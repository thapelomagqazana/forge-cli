"""Structured safety errors — the one error vocabulary for the safety layer.

Where this sits
---------------
Three safety modules inspect different things:

    * `paths.safe_resolve`         — path structure (traversal, symlinks)
    * `destinations.validate_*`    — destination readiness (existence, perms)
    * `collisions.detect_*`        — plan-vs-disk collisions

They all need a *common language* to report what went wrong. This
module is that language. It defines two types:

    * `Finding`       — a single, self-contained problem. Data, not an
                        exception. Callers *aggregate* findings across
                        gates into one report so the user fixes
                        everything in one pass instead of one gate at
                        a time.

    * `SafetyError`   — a group of findings bundled for the CLI/exit-
                        code boundary. Raised *once* by the caller
                        after it has decided the operation should
                        fail. `CollisionError` is a subtype because
                        CI distinguishes "blueprint problem" (2),
                        "template problem" (4), "safety problem" (5),
                        and "collision problem" (6).

Why findings are data
---------------------
Exceptions short-circuit. If `safe_resolve` raised on the first bad
path, a plan with 200 destinations would surface one error per run.
Findings aggregate: one call, one report, all problems. This is the
same discipline used in `loading`, `planning`, and `conditions`, so a
user who understands "how does forge report problems" has one mental
model across the whole CLI.

Why the split between `Finding` and `SafetyError`
-------------------------------------------------
A finding is one problem, and it lives in a list. `SafetyError` is
the *bundle* — a mutable-where-necessary group of findings, tagged
with an operation label — that the caller raises at the boundary.
Keeping them distinct means:

    * inner modules (`paths`, `destinations`, `collisions`) only
      *return* `Finding` values and never raise;
    * boundary code (CLI command, HTTP handler) only *raises*
      `SafetyError` and never inspects findings individually;
    * tests can exercise either layer in isolation.

Why `CollisionError` is its own type
------------------------------------
Collisions are not "safety failures" in the ordinary sense. They are
*recoverable with user consent* — `--overwrite` (with backups) is a
legitimate resolution. Structural safety failures (`..` traversal,
absolute paths) are *not* recoverable by the user — there is no flag
that says "yes, write outside the root". CI needs to distinguish
these to (a) offer the right remediation, and (b) exit with the right
code. Making collision a subtype of `SafetyError` preserves the "it
is a safety failure" relationship for generic handlers, while giving
specific handlers (`--overwrite` flow, exit code 6) a precise type to
match.

Why `Finding` is frozen but `SafetyError` is not
------------------------------------------------
`Finding` is a **value**: once produced, it does not change; two
findings with the same fields are equal; it is safe to sort,
deduplicate, and snapshot-test. Frozen + slots is the right
combination for it, and it never interacts with the exception
machinery (it is never raised, never caught, never has
``__traceback__`` set on it).

`SafetyError` is an **event**: its identity is its traceback, not its
fields, and the Python exception machinery requires
``__traceback__``, ``__context__``, ``__cause__``, and ``__notes__``
to be settable *after* the exception is caught. A frozen dataclass
forbids those assignments, and ``contextlib.__exit__`` (used by
``pytest.raises``, ``unittest``, and every traceback-preserving
library) raises::

    TypeError: super(type, obj): obj must be an instance or subtype of type

when it tries to reattach the traceback. This is the *same* landmine
``forge.core.exceptions``'s module docstring documents under "Mutable
structured errors, not frozen dataclasses" — the safety layer's error
classes were violating the codebase's own policy.

The fix is ``@dataclass(slots=True)`` for the two exception classes
(``slots=True`` stays; it reduces per-instance memory without
interfering with exception mechanics). ``frozen=True`` is gone
because the "value object" property it provides is not worth breaking
the exception machinery.

Why ``__post_init__`` calls ``Exception.__init__`` explicitly
-------------------------------------------------------------
A dataclass-generated ``__init__`` bypasses ``Exception.__init__``.
The subclass still prints correctly because ``__str__`` is overridden
— but ``exc.args`` is empty, pickle round-trips lose the message, and
any library that inspects ``args`` (loggers, some test frameworks)
sees nothing. Calling ``Exception.__init__(self, str(self))`` in
``__post_init__`` populates ``args`` with the rendered message, which
is the only correct value for a message-carrier exception.

Why the ``findings`` field is ``tuple[Finding, ...]`` but the
constructor parameter is ``Iterable[Finding]``
----------------------------------------------
The *field* is a tuple — that is the invariant ``SafetyError``
maintains once constructed, and it is what every method
(``__str__``, ``codes``, ``has_code``) reads. Making the field a
list would allow callers to append after construction, which the
"bundle of findings" semantic forbids.

The *constructor parameter* is ``Iterable[Finding]`` because callers
legitimately have a list or generator in hand: a loop that appends
findings one per bad path is idiomatic Python. Requiring every
caller to wrap their result in ``tuple(...)`` before constructing is
noise that gains nothing — the normalization is cheap, it happens
once, and doing it in ``__init__`` keeps the field's invariant
intact.

An earlier version of this module declared the field as
``tuple[Finding, ...]`` and *also* tried to coerce inside
``__post_init__`` with a runtime ``isinstance`` guard. Mypy strict
correctly flagged the guard's body as ``[unreachable]`` — the
annotation promised a tuple, so mypy reasoned the guard's inner
branch could never run. The annotation and the coercion were
contradictory: the *annotation* declared the post-construction type
while the *coercion* existed precisely because the caller might pass
something the annotation forbids. The fix is an explicit ``__init__``
whose parameter is ``Iterable[Finding]`` — the honest type for what
callers actually pass — that assigns ``self.findings = tuple(findings)``.
Mypy now sees the coercion as reachable (the input type is wider than
the field type), the field stays a tuple, and the invariant
``SafetyError.findings is always a tuple`` holds at runtime.

The explicit ``__init__`` also eliminates the awkwardness the
dataclass-generated one had around exception subclasses: the
dataclass-generated one would have accepted ``tuple[Finding, ...]``
and bypassed ``Exception.__init__``, which ``__post_init__`` then had
to patch up. An explicit ``__init__`` that calls ``__post_init__``
explicitly (because the dataclass machinery only auto-calls it when
it generates ``__init__`` itself) is cleaner *and* lets the type
checker see the coercion.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Finding:
    """A single safety problem, ready to be aggregated into a report.

    Fields
    ------
    code : str
        Machine-readable classifier. Stable across versions (consumers
        may branch on it). Examples:

            path.absolute, path.escapes_root, path.symlink_component,
            path.nul_byte, path.too_long, path.illegal_chars,
            path.inspect_failed, path.unresolvable,
            destination.exists, destination.symlink,
            destination.parent_not_writable, destination.parent_not_directory,
            destination.no_ancestor,
            collision.exists, collision.inspect_failed,
            plan.case_insensitive_duplicate

        Adding a new code is additive; changing an existing one is a
        breaking change and requires an ADR amendment.

    path : str
        The path the finding is *about*. This is always the path the
        user supplied (or, when the finding is about an intermediate
        component, the intermediate path in a form the user can locate
        on their own filesystem). Never an internal-only identifier —
        users fix what they can see.

    message : str
        One-sentence description of *what is wrong*. Written for a
        reader who knows their project but not our internals. No
        stack-trace language, no "expected type X but got Y".

    hint : str
        One sentence on *what to do about it*. This is mandatory —
        every finding has a hint. A hint-less finding is treated as a
        bug in the safety layer; the CLI's error renderer surfaces the
        hint prominently, and CI can assert `all(f.hint for f in
        findings)`.

    root : str
        The root against which `path` was resolved. Empty when the
        finding is not root-scoped (e.g. a NUL-byte check, which fails
        before a root is relevant). Included so a user can see "this
        path, against that root" in the same message — the pairing is
        often the whole story (a path that is safe against one root
        and unsafe against another).

    Immutability
    ------------
    Frozen + slots. Findings are values: once produced, they do not
    change. Two findings with the same fields are equal. That makes
    them safe to sort, deduplicate, set-member, and snapshot-test.
    This is *safe* for `Finding` in a way it would not be for
    `SafetyError` — a finding is never raised, never caught, never
    has `__traceback__` set on it, so the frozen-dataclass +
    exception-machinery interaction does not apply.
    """

    code: str
    path: str
    message: str
    hint: str
    root: str = ""

    def format(self) -> str:
        """One-line, copy-pasteable rendering for CLI and log output.

        Kept as a method (not a free function) so callers can format a
        single finding without importing a formatter module — the
        finding *is* the formatter of itself. The CLI's bulk renderer
        (which handles many findings and section headings) uses this
        per-finding output as a building block.
        """
        loc = self.path or "<root>"
        prefix = f"[{self.code}] {loc}"
        if self.root:
            prefix += f" (root: {self.root})"
        return f"{prefix}: {self.message} — hint: {self.hint}"


@dataclass(slots=True)
class SafetyError(Exception):
    """A bundle of safety findings, raised at the CLI boundary.

    Constructed by the caller *after* it has aggregated findings and
    decided the operation should fail. Inner modules never raise this;
    they only *return* `Finding` values. That split means:

        * `paths.py`, `destinations.py`, `collisions.py` are pure and
          trivially testable (no exception plumbing),
        * the boundary (CLI command, future HTTP handler) owns the
          "raise vs. report" decision,
        * tests for the inner modules need no `pytest.raises`.

    Fields
    ------
    findings : tuple[Finding, ...]
        The problems bundled into this failure. Tuple, not list, so
        the *shape* is stable and two errors with the same findings
        compare equal. The dataclass is not frozen (see the module
        docstring), but the tuple is — so callers cannot accidentally
        append to the findings list through the attribute.

        The *field* is a tuple; the *constructor* accepts any
        ``Iterable[Finding]`` and coerces. See the module docstring's
        last section for why.

    operation : str
        A short label for what was being attempted (`"create"`,
        `"dry-run"`, `"commit"`). Used in the rendered message and in
        logs so a support engineer can correlate the failure with a
        command the user ran. Defaults to `"<unknown>"` so a hastily
        constructed error still renders sensibly.

    Why subclass `Exception`, not a Pydantic BaseModel?
        Because the boundary actually *raises* it. An exception must
        be raisable, chainable, and compatible with `except` clauses.
        A Pydantic model would need to be wrapped or a custom
        `__raise__` convention invented — for no benefit. The fields
        are simple enough that the dataclass is sufficient.

    Why *not* frozen, unlike `Finding`?
        Python's exception machinery sets ``__traceback__`` after
        catching, and a frozen dataclass forbids that assignment. The
        failure surfaces inside ``contextlib.__exit__`` when a caller
        uses ``pytest.raises``, ``unittest.TestCase.assertRaises``, or
        any traceback-preserving library. See the module docstring
        for the full reasoning and the exact ``TypeError`` it
        produces.
    """

    findings: tuple[Finding, ...]
    operation: str = "<unknown>"

    def __init__(
        self,
        findings: Iterable[Finding],
        operation: str = "<unknown>",
    ) -> None:
        """Accept any iterable of findings, normalize to a tuple.

        The parameter is ``Iterable[Finding]`` even though the *field*
        is ``tuple[Finding, ...]``. The reason: callers build findings
        in lists and generators — that is the natural Python idiom for
        a loop that produces one finding per bad path — and forcing
        them to wrap the result in ``tuple(...)`` before constructing
        the exception is busywork that gains nothing. The coercion is
        one allocation, done once, at the moment the exception is
        constructed (which is the moment the caller has decided the
        operation fails). The invariant that ``self.findings`` is
        *always* a tuple is what every method below relies on, and
        this ``__init__`` is the only place that invariant is
        established.

        Why an explicit ``__init__`` and not a dataclass-generated one
        with a runtime guard in ``__post_init__``:

        The dataclass-generated ``__init__`` would type the parameter
        as the *field's* type (``tuple[Finding, ...]``), and a
        subsequent ``isinstance(..., tuple)`` check inside
        ``__post_init__`` would be flagged by mypy strict as
        ``[unreachable]`` — because the annotation promises a tuple.
        That flag is *correct* about the annotation but *wrong* about
        the runtime: the annotation declares the field, not the input.
        Splitting the constructor's parameter type (``Iterable``) from
        the field's type (``tuple``) is the honest shape, and it is
        what makes the coercion reachable to mypy. See the module
        docstring's last section.

        ``__post_init__`` is called explicitly here (not automatically
        by the dataclass framework) because the framework only
        auto-invokes ``__post_init__`` when it generates ``__init__``
        itself. Since this class provides its own ``__init__``, the
        call is explicit — and it is what runs the empty-findings
        check and populates ``Exception.args``.
        """
        # Normalize before assignment so the field invariant (tuple)
        # holds from the first observable moment.
        self.findings = tuple(findings)
        self.operation = operation
        # The dataclass framework does NOT auto-call ``__post_init__``
        # when the class defines its own ``__init__`` — it only does
        # so when it generates ``__init__`` itself. The explicit call
        # is therefore load-bearing, not redundant.
        self.__post_init__()

    def __post_init__(self) -> None:
        """Reject empty findings and populate ``Exception.args``.

        Runs after the explicit ``__init__`` above has normalized
        ``findings`` to a tuple. The normalizations that used to live
        here (the ``isinstance`` check and the ``tuple(...)``
        coercion) now live in ``__init__``, where the parameter's
        ``Iterable`` annotation makes them reachable to mypy.
        """
        if not self.findings:
            raise ValueError(
                "SafetyError requires at least one finding; "
                "raise the underlying condition instead."
            )
        # Populate args so pickle, repr, and libraries that read
        # `exc.args` see the rendered message.
        Exception.__init__(self, str(self))

    def __str__(self) -> str:
        """Human-readable, grouped rendering.

        Chosen over a flat list because the *count* is the first thing
        a user needs: "you have 4 problems" tells them whether to fix
        one thing or many. Then each finding is rendered via its own
        `format()` method, so per-finding rendering is not duplicated
        between single and bulk contexts.
        """
        n = len(self.findings)
        lines = [f"{n} safety issue(s) during {self.operation}:"]
        for f in self.findings:
            lines.append(f"  • {f.format()}")
        return "\n".join(lines)

    def format(self) -> str:
        """Alias for `str()`, matching the convention on `Finding`.

        Some call sites (loggers, JSON encoders) call `.format()`
        explicitly; others rely on `str()`. Providing both keeps the
        call sites honest without requiring them to know which
        convention this module chose.
        """
        return str(self)

    def codes(self) -> tuple[str, ...]:
        """The distinct finding codes in this error, in stable order.

        Convenience for callers that need to *classify* rather than
        *render* — e.g. "does this error contain any collision?" or
        "should I offer --overwrite?". The result is sorted and
        deduplicated so equality checks are meaningful.
        """
        return tuple(sorted({f.code for f in self.findings}))

    def has_code(self, code: str) -> bool:
        """True iff any finding carries the given code.

        Thin wrapper over `codes()`; exists because `code in err.codes()`
        reads more clearly at call sites and lets us add caching later
        without touching callers.
        """
        return any(f.code == code for f in self.findings)


@dataclass(slots=True)
class CollisionError(SafetyError):
    """A subset of `SafetyError` scoped to collisions.

    Semantics
    ---------
    A collision is *recoverable with user consent* — `--overwrite`
    (which creates backups) is a legitimate resolution. This is
    different from structural safety failures (traversal, absolute
    paths) where no consent flag can make the operation safe.
    Callers use this type to:

        * offer `--overwrite` specifically for collisions, not for
          traversal attempts (which `--overwrite` cannot fix),
        * exit with code 6 rather than 5, so CI can distinguish "the
          user needs to accept an overwrite" from "the plan is unsafe
          and needs editing",
        * render a "use --overwrite" hint (the finding's hint already
          contains this, but a caller may want to say it once at the
          top of the report rather than once per finding).

    The class adds no fields. It is a *marker subtype* — the only
    thing that distinguishes it is the ability to `isinstance()`-check
    and to have the exit code derived from the type. Adding fields
    would tempt callers to construct it inconsistently; a marker is
    the smallest type that carries the semantic.

    Why not frozen, like its parent ``SafetyError``?
        Same reason as the parent: the exception machinery sets
        ``__traceback__`` after catching, and a frozen dataclass
        forbids that. See the module docstring.

    Why no ``__init__`` override?
        The parent's ``__init__`` (which takes ``Iterable[Finding]``
        and normalizes to a tuple) is exactly what this subclass
        needs. Dataclasses propagate ``__init__`` through the MRO, so
        a subclass that does not override it inherits the parent's
        normalization. That is the desired behaviour — a
        ``CollisionError`` built from a list of collision findings
        gets the same coercion a ``SafetyError`` does.

    Why no ``__post_init__`` override?
        Same reasoning as ``__init__`` — the parent's
        ``__post_init__`` (reject empty, populate ``Exception.args``)
        is exactly what this subclass needs, and the subclass's
        inherited ``__init__`` calls it explicitly. A
        ``super().__post_init__()`` override would introduce the
        ``slots=True`` + ``super()`` landmine this module documents
        for no benefit.
    """
