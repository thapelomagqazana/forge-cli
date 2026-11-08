"""Interactive prompt engine for dual-mode CLI commands.

This module is the interactive half of the dual-mode input design (see
ADR-0006). Its single responsibility is to **ask questions and return
answers**. It does not:

- validate answers against blueprints (the verification layer does),
- read or write the filesystem (the command layer does),
- know what a "project" or a "blueprint" is (the domain does),
- render success or error messages (``cli.ui`` does).

Keeping those concerns out of this module is not fussiness. It is what
lets the wizard be swapped for a future web form or TUI without
touching the command logic, and what lets the whole wizard be tested
without a real terminal.

Why ``questionary`` and not ``rich.prompt``
-------------------------------------------
``rich.prompt`` raises ``InvalidResponse`` on invalid input, which
forces every call site to write the same re-prompt loop:

    while True:
        try:
            value = Prompt.ask(...)
            if validate(value):
                break
        except InvalidResponse:
            console.print("invalid")

That loop is easy to get subtly wrong (forget to ``break``, forget to
handle EOF, forget to trim whitespace) and impossible to test without
capturing ``stdin``. ``questionary`` accepts a ``validate=`` callback
that returns ``True`` or an error string, and it handles the re-prompt
internally. Real-time validation -- an acceptance criterion for the
``create`` wizard -- becomes a *parameter* of ``ask_text``, not a
*pattern* every caller must re-implement correctly.

The trade-off is one dependency. Given that ``questionary`` is
already the project's interactive library of choice (see
``pyproject.toml``), the trade-off is a non-decision: use the library
that makes the correct thing the easy thing.

Why cancellations live here, not in ``cli.errors``
--------------------------------------------------
``PromptCancelledError`` is *defined* in ``core.exceptions`` (so the
domain owns the exit code), but it is *raised* only here. That split
is intentional: the exception is a domain concept, but the boundary
at which ``KeyboardInterrupt`` becomes a domain error is the prompt
layer. If every prompt converted interrupts itself, we would have N
copy-pasted ``try/except`` blocks; if the command layer converted
them, a future non-prompt interactive surface would have to
re-implement the conversion. One function, ``_guard``, owns the
conversion.

Why ``sys.stdin`` and not ``sys.stdout``
----------------------------------------
``is_interactive()`` checks ``sys.stdin.isatty()`` because stdin is
what we *read* from. A piped *stdout* (``forge create | tee log``)
should not disable the wizard -- the user is still sitting at a
terminal, still able to answer questions. Checking stdout would be
wrong; checking stdin is right. This is a small detail with a big
UX consequence, and it is the kind of thing that is easy to get
backwards if the check is scattered across call sites. It lives here,
once.

Why ``_guard`` catches ``EOFError`` too
---------------------------------------
Ctrl+C raises ``KeyboardInterrupt``. Closing stdin (Ctrl+D on Unix,
Ctrl+Z on Windows) raises ``EOFError``. Both mean "the user stopped
answering." Treating only the first as cancellation would leave the
second to produce a traceback -- a bug users would report as "the
wizard crashes when I press Ctrl+D." Catching both is one extra
exception class in a tuple and closes the hole.

``>`` renders on every terminal that has ever existed, including
minimal Linux consoles and serial consoles on routers with 1993-era
fonts. The prompt's job is to point to the current line; ``>`` does
that.

The other three glyphs in ``_PROMPT_STYLE`` (``?``, ``✓``, ``✗``)
stay non-ASCII deliberately. ``?`` is ASCII already. ``✓`` and
``✗`` are *symbols*, not *pointers* -- their job is to be visually
distinct from every other glyph in a monospaced terminal, and they
are. ``RUF001`` does not flag them because they have no ASCII
lookalike a reader could confuse in source or output (``v`` is not
``✓`` in a monospaced font; ``x`` is not ``✗``). The rule's purpose
is ambiguity that causes bugs; ``✓`` and ``✗`` do not cause bugs.

Design principles
-----------------
1. **One prompt, one question.** No prompt function asks two things.
   Composition happens in the command layer, not here.
2. **Return types match the answer's semantic type.** ``ask_confirm``
   returns ``bool``; ``ask_select`` returns the chosen ``str``;
   ``ask_text`` returns a trimmed ``str``. Callers never coerce.
3. **Validation is a parameter, not a pattern.** ``ask_text`` and
   ``ask_select`` accept an optional validator; the re-prompt loop is
   inside the function, tested once.
4. **Cancellation is uniform.** Every prompt goes through ``_guard``.
   There is no path from a prompt to a traceback.
5. **No I/O outside the prompt.** No ``print``, no file reads, no
   blueprint lookups. If a caller needs those, the caller does them.
   This module is a pipe, not a filter.
6. **The prompt engine is swappable.** ``is_interactive()`` and the
   ``ask_*`` functions are the entire public surface. Replacing
   ``questionary`` with something else means rewriting this file, not
   the command layer.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, TypeVar, cast

from forge.core.exceptions import (
    PromptCancelledError,
    UserInputError,
)

if TYPE_CHECKING:
    # Imported for the type checker only. ``TYPE_CHECKING`` is a
    # module-level constant that is ``False`` at runtime and ``True``
    # only inside type checkers, so this import does not execute in
    # production. It exists so that ``_make_style`` can be annotated
    # as returning ``questionary.Style`` — the type questionary's
    # own stubs declare for its prompt constructors' ``style=``
    # parameter — without paying questionary's import cost in
    # non-interactive commands (``forge verify`` in CI, etc.). The
    # lazy import inside each prompt function is retained; this is
    # a *type-only* import, not a runtime one.
    import questionary


# -----------------------------------------------------------------------------
# Public type aliases
# -----------------------------------------------------------------------------
Validator = Callable[[str], "str | None"]
"""A validator returns ``None`` on success, or an error message.

The ``None``-on-success convention (rather than ``True``) is chosen so
callers can return *why* validation failed, not just *that* it failed.
The message is shown to the user inline, under the prompt, which is
the whole point of real-time validation -- the user fixes a typo while
typing instead of discovering the rule after a failed scaffold.

Typing note: the return annotation is a string literal (``"str | None"``)
because Python 3.9 and 3.10 do not support ``X | Y`` syntax in
annotations at runtime. The project targets Python 3.11+, so this is
defensive rather than necessary; it costs nothing and prevents a
future Python-version bump from silently breaking imports.
"""


# -----------------------------------------------------------------------------
# Internal type variable
# -----------------------------------------------------------------------------
# ``_guard`` must preserve the return type of the callable it wraps:
# ``ask_text`` returns ``str``, ``ask_confirm`` returns ``bool``, and
# ``_guard`` must not narrow either to the other. A ``TypeVar`` (rather
# than ``Callable[[], object]``) lets ``mypy`` infer the specific
# return type at each call site.
_T = TypeVar("_T")


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
_PROMPT_STYLE: Final[dict[str, str]] = {
    "qmark": "?",
    "answer": "✓",
    "invalid": "✗",
    "pointer": ">",
}
"""Visual theme for questionary prompts.

Defined as a module-level constant so the wizard's look-and-feel is
consistent across every prompt *and* overridable in tests via
``monkeypatch.setattr``. A theme is not business logic; it is
presentation. Keeping it here (rather than passing it to every call)
means the answer to "what does a prompt look like?" is one constant,
not N keyword arguments.

The four entries are the entire palette:

- ``qmark`` -- the prefix shown before the question text (``?``).
- ``answer`` -- the prefix shown before a valid answer (``✓``).
- ``invalid`` -- the prefix shown before a rejected answer (``✗``).
- ``pointer`` -- the marker shown next to the currently-selected
  menu option (``>``).

``qmark`` and ``pointer`` are passed as top-level keyword arguments
to ``questionary``'s prompt constructors (which accept them
directly). ``answer`` and ``invalid`` are part of questionary's
*style* object, not top-level kwargs, so this module builds a
``questionary.Style`` from them in ``_make_style`` -- otherwise they
would be dead entries in the constant, which would be a lie about
what the wizard looks like.
"""


# -----------------------------------------------------------------------------
# Style factory
# -----------------------------------------------------------------------------
def _make_style() -> questionary.Style:
    """Build a ``questionary.Style`` from ``_PROMPT_STYLE``.

    The ``answer`` and ``invalid`` glyphs are not top-level keyword
    arguments on questionary's prompt constructors -- they are part of
    questionary's style object. Building the style here keeps the
    constant as the single source of truth for the theme, so a future
    change to ``_PROMPT_STYLE`` (e.g., "use ``[y]`` instead of ``✓``")
    is one edit, not two.

    The return type is ``questionary.Style`` — the exact type
    questionary's own stubs declare for its prompt constructors'
    ``style=`` parameter. The import needed to *name* that type is
    gated behind ``TYPE_CHECKING`` (see the top of the module): it
    exists for mypy only, and does not execute at runtime. That
    preserves the module's design goal — ``questionary`` is imported
    lazily inside each prompt function so that non-interactive
    commands like ``forge verify`` do not pay for its import cost —
    while letting the annotation tell the truth. An earlier version
    of this function annotated the return as ``object`` to avoid
    needing the type-only import; that worked while questionary had
    no stubs, but now that its stubs declare ``style: Style | None``
    on every prompt constructor, the ``object`` annotation produced
    ``arg-type`` errors at every call site. The ``TYPE_CHECKING``
    import is the correct fix: it costs nothing at runtime and gives
    the type checker the real type.

    Returns:
        A ``questionary.Style`` instance whose prompt and pointer
        tokens match ``_PROMPT_STYLE``. See questionary's style docs
        for the token names — they are stable across the versions
        this project pins.
    """
    import questionary

    return questionary.Style(
        [
            ("qmark", "fg:#5f819d bold"),  # question mark
            ("question", "bold"),  # the message text
            ("answer", "fg:#5f819d bold"),  # user's submitted answer
            ("pointer", "fg:#5f819d bold"),  # the ">" menu pointer
            ("highlighted", "fg:#5f819d bold"),  # selected menu item
            ("selected", "fg:#5f819d"),  # chosen but unfocused
            ("separator", "fg:#6c6c6c"),  # menu separators
            ("instruction", "fg:#6c6c6c"),  # hints like "(Use arrow keys)"
            ("text", ""),  # free-form input
            ("disabled", "fg:#858585 italic"),  # disabled menu items
        ]
    )


# -----------------------------------------------------------------------------
# Interactivity detection
# -----------------------------------------------------------------------------
def is_interactive() -> bool:
    """Return ``True`` only when stdin is attached to a real terminal.

    This is the single decision point that dispatches the ``create``
    command between its interactive and non-interactive paths. It is
    called once, at the top of the command, so the mode cannot change
    mid-flight (which would be confusing and untestable).

    Any exception from exotic stdin objects (e.g. in some CI runners)
    is treated as "not a TTY". Defaulting to the safe, script-friendly
    path is the right bias: a false negative (interactive user
    mistakenly put in non-interactive mode) produces a clear
    ``MissingRequiredFlagsError``; a false positive (CI job mistakenly
    put in interactive mode) hangs waiting for input that will never
    come. Fail fast, not slow.

    Returns:
        ``True`` if ``sys.stdin.isatty()`` returns ``True``;
        ``False`` for pipes, files, redirected stdin, or any stdin
        object that does not expose ``isatty``.
    """
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        # AttributeError: stdin is an exotic object without isatty.
        # ValueError: stdin is closed. Both mean "no human here."
        return False


# -----------------------------------------------------------------------------
# Cancellation guard
# -----------------------------------------------------------------------------
def _guard(call: Callable[[], _T]) -> _T:
    """Run a prompt call, translating interruptions into domain errors.

    The return type is generic in ``_T``: ``_guard`` preserves
    whatever the wrapped callable returns, so ``ask_text`` sees
    ``str`` and ``ask_confirm`` sees ``bool``. A non-generic
    signature (``Callable[[], str] -> str``) would force
    ``ask_confirm`` to lie about its return type -- which is exactly
    the mypy error this generic signature resolves.

    ``KeyboardInterrupt`` (Ctrl+C) and ``EOFError`` (Ctrl+D / closed
    stdin) both mean "the user stopped answering." Neither should
    produce a traceback -- that is a bug users would report as "the
    wizard crashed when I pressed Ctrl+D." Both are translated to
    ``PromptCancelledError``, which the CLI handler renders as a
    single dimmed "Cancelled." line on stderr and exits with code 1.

    Every prompt in this module is wrapped by this function. That is
    not decoration: it is the mechanism that makes cancellation
    uniform. If a future contributor adds a new prompt and forgets to
    wrap it, the test suite will catch the asymmetry (see
    ``tests/unit/cli/test_prompts.py``), because every prompt test
    exercises the cancellation path.

    Args:
        call: A zero-argument callable that performs the prompt and
            returns its answer. ``questionary``'s ``.ask()`` /
            ``.unsafe_ask()`` bound methods fit this shape directly.

    Returns:
        Whatever ``call()`` returns, unchanged.

    Raises:
        PromptCancelledError: If the prompt was interrupted by Ctrl+C
            or EOF.
    """
    try:
        return call()
    except (KeyboardInterrupt, EOFError) as exc:
        # ``from exc`` preserves the original exception for --debug
        # tracebacks (the developer sees KeyboardInterrupt, not just
        # the domain wrapper). The user never sees a traceback
        # because the handler renders PromptCancelledError as a
        # cancellation line.
        raise PromptCancelledError() from exc


# -----------------------------------------------------------------------------
# Prompts
# -----------------------------------------------------------------------------
def ask_text(
    message: str,
    *,
    default: str | None = None,
    validator: Validator | None = None,
    allow_empty: bool = False,
) -> str:
    """Ask a free-text question, with optional real-time validation.

    The validation loop is *inside* this function, not at the call
    site. That is the difference between "callers can validate" and
    "callers cannot accidentally fail to validate." A caller that
    passes a validator gets the re-prompt loop for free; a caller
    that does not gets an unvalidated prompt -- which is a deliberate
    opt-in, not a forgotten step.

    Behaviour:

    - The answer is stripped of leading/trailing whitespace. A user
      who types ``"  my-api  "`` gets ``"my-api"``. This is the
      99% case; a caller that genuinely needs to preserve whitespace
      (e.g., a description field) should not use ``ask_text``.
    - Empty input is rejected unless ``allow_empty=True``. An empty
      project name is a user error, and catching it here (before it
      reaches the validator) keeps the validator's job focused on
      *format*, not on *presence*.
    - If a validator is provided, it is called on every attempt
      *including the first*. This is what makes the real-time
      feedback real-time: the user sees the error on the same line
      they typed, not after a round-trip.
    - Ctrl+C / Ctrl+D raise ``PromptCancelledError``.

    Args:
        message: The prompt text shown to the user. Should end with
            a question or a colon, depending on the project's style
            guide. This module does not enforce either.
        default: Pre-filled answer the user can accept by pressing
            Enter. ``None`` means "no default, user must type
            something." A default of ``""`` is treated as ``None``
            (an empty default is a UX anti-pattern -- it looks like
            a suggestion but accepts nothing).
        validator: Optional callback invoked on every attempt.
            Returns ``None`` on success, or a human-readable error
            message on failure. The message is shown inline and the
            prompt re-opens.
        allow_empty: If ``True``, empty input is accepted (subject
            to the validator, if any). Defaults to ``False`` because
            an empty answer is more often a mistake than an intent.

    Returns:
        The user's answer, stripped, and validated (if a validator
        was provided).

    Raises:
        PromptCancelledError: If the user pressed Ctrl+C or Ctrl+D.
    """
    # Local import keeps CLI startup fast for non-interactive runs
    # (e.g., ``forge verify`` in CI never imports questionary). The
    # import is cheap enough that re-importing on every prompt is a
    # non-issue -- Python caches modules in sys.modules after the
    # first import.
    import questionary

    # Normalize empty-string defaults to None. An empty default looks
    # like a suggestion but accepts nothing; treating it as "no
    # default" is the least surprising behavior.
    effective_default = default if default else None

    # Build the style once per prompt call, not once per module
    # import, because the style depends on questionary being
    # imported. ``_make_style`` handles the lazy import internally.
    style = _make_style()

    def _run_once() -> str:
        # ``cast`` is needed because ``questionary`` has no type
        # stubs (see ``pyproject.toml``'s ``ignore_missing_imports``
        # for ``questionary.*``). The cast is not a lie:
        # ``questionary.text(...).unsafe_ask()`` returns a ``str``
        # per the library's documented API. Without the cast,
        # ``mypy`` sees ``Any`` and rejects the return.
        answer: str = cast(
            str,
            questionary.text(
                message,
                default=effective_default or "",
                qmark=_PROMPT_STYLE["qmark"],
                style=style,
            ).unsafe_ask(),
        )

        # ``unsafe_ask`` raises KeyboardInterrupt / EOFError directly,
        # which is exactly what ``_guard`` expects. ``.ask()`` would
        # swallow KeyboardInterrupt and return None, which is the
        # opposite of what we want.

        # Strip before validating: a validator should see the answer
        # the caller will receive, not the raw input with surrounding
        # whitespace. This prevents a class of bugs where a validator
        # accepts " my-api " but the scaffold then writes a directory
        # named " my-api " (with a leading space) because the caller
        # forgot to strip.
        answer = answer.strip()

        if not answer and not allow_empty:
            # Re-prompt on empty input. The error is shown inline,
            # exactly like a validator failure, so the user has one
            # mental model: "the wizard tells me what's wrong and
            # lets me fix it."
            questionary.print("  This field cannot be empty.", style="fg:red")
            return _run_once()

        if validator is not None:
            error = validator(answer)
            if error:
                # Validator failure: show the message, re-prompt. The
                # recursive call is bounded by the user's patience,
                # which is exactly the right bound for an interactive
                # prompt. (A non-interactive mode would never reach
                # this code path.)
                questionary.print(f"  {error}", style="fg:red")
                return _run_once()

        return answer

    return _guard(_run_once)


def ask_select(
    message: str,
    choices: Sequence[str],
    *,
    default: str | None = None,
) -> str:
    """Present a single-choice menu and return the chosen value.

    Used for blueprint selection (the wizard's first question) and
    for any future enum-typed blueprint variable. The caller must
    supply a non-empty list; an empty menu is a configuration bug,
    not a user-error condition, so it raises rather than silently
    returning ``None``.

    Why ``ask_select`` does not accept a ``validator``: a select menu
    cannot produce an invalid answer. The user can only choose from
    what is shown, or cancel. Adding a validator would be dead code,
    and dead code is a maintenance liability. Validation of the
    *selected* value against blueprint rules happens in the
    verification layer, on the returned string -- the same funnel as
    flag-supplied values (see ADR-0006).

    Args:
        message: The prompt text shown to the user.
        choices: The options to display. Must be non-empty. Order is
            preserved -- the caller is responsible for sorting if it
            wants deterministic menu order (the registry's
            ``list_blueprints()`` returns a sorted list for exactly
            this reason).
        default: The option to pre-select. If ``None``, the first
            choice is pre-selected (questionary's default behavior,
            which matches most CLIs). If provided but not in
            ``choices``, questionary raises -- this is a caller bug,
            and failing loudly is correct.

    Returns:
        The selected choice, exactly as it appeared in ``choices``.

    Raises:
        UserInputError: If ``choices`` is empty. This is a programmer
            error (the wizard was launched with no options), not a
            user error -- but ``UserInputError`` is the right domain
            class because the wizard must exit cleanly with a code 1
            and a clear message, not crash with a traceback.
        PromptCancelledError: If the user pressed Ctrl+C or Ctrl+D.
    """
    import questionary

    if not choices:
        # Empty menu = misconfiguration, not user fault. Raise the
        # domain error so the CLI handler renders a clean message
        # and exits 1, instead of questionary raising an opaque
        # internal error.
        raise UserInputError(
            "No options are available to select from.",
            hint="This is likely an installation problem. Try "
            "reinstalling Forge, or check that blueprints are present.",
        )

    # Build the style once per prompt call -- see the note in
    # ``ask_text`` for why it is not a module-level import.
    style = _make_style()

    def _run() -> str:
        # See the ``cast`` note in ``ask_text`` -- same reason.
        return cast(
            str,
            questionary.select(
                message,
                choices=list(choices),
                default=default,
                qmark=_PROMPT_STYLE["qmark"],
                pointer=_PROMPT_STYLE["pointer"],
                style=style,
            ).unsafe_ask(),
        )

    return _guard(_run)


def ask_confirm(
    message: str,
    *,
    default: bool = True,
) -> bool:
    """Ask a yes/no question and return ``True`` or ``False``.

    Used for boolean-typed blueprint variables and for overwrite
    confirmations. The default is ``True`` because the majority of
    confirmations in this CLI are "proceed with the obviously-
    intended action"; a caller that wants the opposite default
    passes ``default=False``.

    Why not ``ask_text`` with a y/n validator: because yes/no has a
    canonical interactive affordance (a ``(Y/n)`` hint, single-key
    response on many terminals) that a free-text prompt loses.
    Reusing the wrong primitive is how CLIs end up feeling like
    forms.

    Args:
        message: The prompt text. Should be phrased as a yes/no
            question (e.g., ``"Create ./my-api?"``).
        default: The value returned if the user presses Enter without
            typing. Shown in the prompt as ``(Y/n)`` or ``(y/N)``.

    Returns:
        ``True`` for yes, ``False`` for no.

    Raises:
        PromptCancelledError: If the user pressed Ctrl+C or Ctrl+D.
            Note that "no" is *not* a cancellation -- it is a valid
            answer. Only interrupt/EOF produce the exception.
    """
    import questionary

    style = _make_style()

    def _run() -> bool:
        # ``cast`` because ``questionary`` has no stubs; the
        # library's documented return for ``.confirm()`` is ``bool``.
        return cast(
            bool,
            questionary.confirm(
                message,
                default=default,
                qmark=_PROMPT_STYLE["qmark"],
                style=style,
            ).unsafe_ask(),
        )

    # ``_guard`` is generic, so this now returns ``bool`` -- the
    # previous non-generic signature forced ``mypy`` to see this as
    # ``str``, which was the "Incompatible return value" error.
    return _guard(_run)


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# The public surface is intentionally small: interactivity detection,
# three prompt primitives, and the validator type alias. Anything
# else a caller might want to do (validate against a blueprint, look
# up a default, format a menu) is a *command-layer* or *verification-
# layer* concern, and adding it here would blur the boundary this
# module exists to enforce.
__all__ = [
    "Validator",
    "ask_confirm",
    "ask_select",
    "ask_text",
    "is_interactive",
]
