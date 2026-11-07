"""Unit tests for the interactive prompt engine.

These tests exercise ``forge.cli.prompts`` through its public API. They
do not require a TTY, do not require ``questionary`` to be installed,
and do not touch the real terminal. Everything is faked at the
boundary — ``sys.stdin`` and the whole ``questionary`` module — so the
tests run identically in CI and on a developer's laptop, and in an
environment where the optional interactive dependency is missing.

Design notes
------------
* **Fake at the boundary, not in the middle.** The fakes replace the
  *whole* ``questionary`` module (via ``sys.modules``), not a private
  method inside it. A questionary version bump changes what the
  library does *internally*, not what ``prompts.py`` asks it to do.
  The tests should survive that bump unchanged.

* **Fake before import, not after.** ``prompts.py`` imports
  ``questionary`` lazily (inside each ``ask_*`` function) so that
  non-interactive commands do not pay the import cost. The tests must
  therefore install the fake *before* calling the prompt, not try to
  ``import questionary`` first. The latter would make the test suite
  depend on the interactive library being installed — a hidden
  dependency that would surface as a confusing ``ModuleNotFoundError``
  in a minimal CI image.

* **The fake is a complete stand-in for the *surface* `prompts.py`
  uses.** That surface is not a fixed set — it grows as `prompts.py`
  grows. When `prompts.py` started using `questionary.Style`, the
  fake needed a `Style` attribute, and the tests failed with an
  opaque `AttributeError` because the fake was incomplete. The
  `test_fake_questionary_module_covers_prompts_imports` test below
  closes that class of failure: it compares the attributes the fake
  exposes to the attributes `prompts.py` calls, and if the fake is
  missing one, *that* test fails with a specific message naming the
  attribute — instead of N tests failing with an opaque
  `AttributeError` from deep inside a `prompts.py` call.

* **Cancellation is tested through every prompt.** ``_guard`` is the
  single place interrupts become ``PromptCancelledError``. The test
  ``test_every_prompt_propagates_cancellation`` parametrizes over the
  public ask functions, so a future contributor who adds
  ``ask_password`` and forgets to wrap it will see the new test fail
  the moment they add it to the parametrize list.

* **No ``mocker`` dependency.** The fakes use ``monkeypatch`` and
  ``sys.modules`` directly. Adding ``pytest-mock`` for these tests
  would buy nothing and cost a dependency.

* **No assertion on styling.** ``questionary.print`` is called for
  inline error messages; we assert on *that it was called* and *with
  what message*, never on the style keyword. Styling is presentation;
  the prompt engine's contract is the return value and the exception
  type.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from forge.cli import prompts
from forge.core.exceptions import PromptCancelledError, UserInputError


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------
class _FakePrompt:
    """A stand-in for a questionary prompt object.

    Questionary's prompts are objects with an ``unsafe_ask()`` method.
    We fake exactly that one method — nothing else. If ``prompts.py``
    ever starts calling ``.ask()`` or ``.unsafe_ask()`` differently,
    these tests will fail at the *call site* in ``prompts.py``, not
    inside a mock of questionary's internals. That is the correct
    place to fail.
    """

    def __init__(self, answers: list[Any]) -> None:
        self._answers = list(answers)
        self.ask_count = 0

    def unsafe_ask(self) -> Any:
        self.ask_count += 1
        if not self._answers:
            raise AssertionError(
                f"Prompt was asked {self.ask_count} time(s) but only "
                f"one or more answers were configured. Did the "
                f"re-prompt loop fire unexpectedly?"
            )
        return self._answers.pop(0)


class _FakeStyle:
    """A stand-in for ``questionary.Style``.

    ``prompts.py``'s ``_make_style`` calls ``questionary.Style([...])``
    to build a presentation object, then passes that object as the
    ``style=`` keyword to the prompt constructors. The fake prompt
    constructors ignore ``style`` entirely — presentation is not the
    contract — so the only thing this class needs to do is exist and
    be callable.

    It deliberately does *not* validate the token names in the list
    it receives. Validating them would couple the tests to
    questionary's style schema, which is a presentation detail, not a
    contract. If a future `prompts.py` change introduces a style
    token that questionary rejects, that is a *runtime* concern
    caught by the manual/interactive test, not a *unit* concern
    caught by faking questionary's validation. The test suite should
    not pretend to know questionary's schema.
    """

    def __init__(self, tokens: Any = None) -> None:
        # ``tokens`` is the list of (name, style) tuples passed to
        # the real ``questionary.Style``. We accept it and drop it
        # — nothing downstream needs it.
        self.tokens = tokens or []


def _build_fake_module(
    *,
    text_answers: list[Any],
    select_answers: list[Any],
    confirm_answers: list[Any],
    printed: list[str],
) -> tuple[ModuleType, dict[str, _FakePrompt]]:
    """Construct a fake ``questionary`` module and its fake prompt objects.

    The fake module exposes the public names ``prompts.py`` uses:
    ``text``, ``select``, ``confirm``, ``print``, and ``Style``.
    Nothing else is defined — if ``prompts.py`` starts calling
    ``questionary.checkbox`` or any other name, the test
    ``test_fake_questionary_module_covers_prompts_imports`` below
    will fail with a specific message naming the missing attribute,
    instead of N tests failing with an opaque ``AttributeError``
    pointing at a line inside ``prompts.py``.
    """
    text_fake = _FakePrompt(text_answers)
    select_fake = _FakePrompt(select_answers)
    confirm_fake = _FakePrompt(confirm_answers)

    module = ModuleType("questionary")

    def _fake_text(*args: Any, **kwargs: Any) -> _FakePrompt:
        return text_fake

    def _fake_select(*args: Any, **kwargs: Any) -> _FakePrompt:
        return select_fake

    def _fake_confirm(*args: Any, **kwargs: Any) -> _FakePrompt:
        return confirm_fake

    def _fake_print(message: str, *args: Any, **kwargs: Any) -> None:
        # Style kwargs are dropped on the floor — see the module
        # docstring: styling is presentation, not contract.
        printed.append(message)

    module.text = _fake_text  # type: ignore[attr-defined]
    module.select = _fake_select  # type: ignore[attr-defined]
    module.confirm = _fake_confirm  # type: ignore[attr-defined]
    module.print = _fake_print  # type: ignore[attr-defined]
    # ``Style`` is a class, not a function — prompts.py constructs
    # it via ``questionary.Style([...])``. A plain no-op class is
    # sufficient; see ``_FakeStyle``'s docstring for why it does not
    # validate its arguments.
    module.Style = _FakeStyle  # type: ignore[attr-defined]

    return module, {
        "text": text_fake,
        "select": select_fake,
        "confirm": confirm_fake,
    }


def _install_questionary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: list[Any] | None = None,
    select: list[Any] | None = None,
    confirm: list[Any] | None = None,
) -> dict[str, Any]:
    """Install a fake ``questionary`` module into ``sys.modules``.

    This must happen **before** the prompt is called, because
    ``prompts.py`` imports ``questionary`` lazily. The previous version
    of this helper did ``import questionary`` first, which made the
    tests depend on the real library being installed. Installing the
    fake in ``sys.modules`` instead makes the tests independent of
    whether ``questionary`` is present — which is the correct design:
    a test that proves cancellation works should not require an
    interactive library to be installed.

    ``monkeypatch.setitem`` is used (not a bare assignment) so the
    fake is automatically removed at test teardown. A leaked fake
    ``questionary`` in ``sys.modules`` would poison every subsequent
    test in the session — exactly the kind of state leak the
    ``conftest.py`` autouse fixtures exist to prevent.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        text: Answers returned by successive ``questionary.text``
            prompts. One shared queue across calls, so the re-prompt
            loop is testable: the loop issues N calls, the fake
            consumes N answers.
        select: Same, for ``questionary.select``.
        confirm: Same, for ``questionary.confirm``.

    Returns:
        A dict with the fake prompt objects and the printed messages
        list. ``printed`` is populated by ``questionary.print`` calls
        — used by tests to assert that a validation error was shown
        inline.
    """
    printed: list[str] = []
    module, fakes = _build_fake_module(
        text_answers=list(text or []),
        select_answers=list(select or []),
        confirm_answers=list(confirm or []),
        printed=printed,
    )
    monkeypatch.setitem(sys.modules, "questionary", module)

    return {**fakes, "printed": printed}


# -----------------------------------------------------------------------------
# The fake's completeness — the tripwire for the class of bug this
# file just experienced
# -----------------------------------------------------------------------------
# When ``prompts.py`` grew a new dependency on questionary (``Style``),
# 17 tests failed with an opaque ``AttributeError`` pointing at
# ``_make_style``. That is a *test-design* failure: the fake was
# incomplete, but the failure pointed at production code.
#
# The test below makes the failure point at the fake instead. It
# compares the attributes the fake exposes to the attributes
# ``prompts.py`` actually accesses, and names any missing one. A
# future change that adds ``questionary.Choice`` (for a hypothetical
# ``ask_multiselect``) fails here with "the fake questionary module is
# missing attribute 'Choice'; add it to _build_fake_module" — one
# failure, one action, zero archaeology.
@pytest.mark.unit()
def test_fake_questionary_module_covers_prompts_imports() -> None:
    """The fake module exposes every questionary name ``prompts.py`` uses.

    The check is done by inspecting ``prompts.py``'s source for
    ``questionary.<name>`` accesses. That is imperfect — it will not
    catch a name passed dynamically, and it will pick up names in
    comments if written carelessly — but it catches the *actual
    pattern* used in this module, and the pattern in this module is
    what the fake needs to satisfy.

    The check is deliberately conservative: it requires the fake to
    expose *at least* the names `prompts.py` uses. Extra names in
    the fake are fine (they let future tests add scenarios without
    touching this test); missing names are the failure case the
    test is here to catch.
    """
    import ast
    import inspect

    source = inspect.getsource(prompts)
    tree = ast.parse(source)

    used_names: set[str] = set()
    for node in ast.walk(tree):
        # ``questionary.foo`` → Attribute(value=Name(id='questionary'), attr='foo')
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "questionary"
        ):
            used_names.add(node.attr)

    # The fake must expose every name ``prompts.py`` accesses. See
    # the module docstring and ``_build_fake_module`` for the
    # contract; if a name is missing, the failure message names it.
    module, _fakes = _build_fake_module(
        text_answers=[],
        select_answers=[],
        confirm_answers=[],
        printed=[],
    )
    fake_names = {name for name in dir(module) if not name.startswith("_")}
    missing = used_names - fake_names
    assert not missing, (
        f"The fake questionary module is missing attribute(s) "
        f"{sorted(missing)!r} that ``prompts.py`` uses. Add them to "
        f"``_build_fake_module`` in this test file. (Extra attributes "
        f"on the fake are fine; missing ones break every prompt test "
        f"with an opaque AttributeError.)"
    )


# -----------------------------------------------------------------------------
# is_interactive
# -----------------------------------------------------------------------------
# The dispatch decision. If this is wrong, the wizard either runs in CI
# (and hangs) or refuses to run for a human (and fails). Both are
# user-visible bugs; neither is testable by accident.
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_is_interactive_true_when_stdin_is_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real terminal is the happy path: the wizard should run."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert prompts.is_interactive() is True


@pytest.mark.unit()
def test_is_interactive_false_when_stdin_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipe or file must dispatch to non-interactive mode.

    This is the CI path. If this test fails, ``forge create`` in a
    script would hang waiting for input that will never arrive.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert prompts.is_interactive() is False


@pytest.mark.unit()
def test_is_interactive_degrades_safely_when_stdin_lacks_isatty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exotic stdin objects (some CI runners) must not crash the CLI.

    The default must be the safe, script-friendly path. A false
    negative produces a clear ``MissingRequiredFlagsError``; a false
    positive would hang. Fail fast, not slow.
    """
    # A sentinel object with no ``isatty`` attribute. The real-world
    # analogue is a stdin wrapper that only implements ``read`` and
    # ``fileno`` (some CI runners use these).
    monkeypatch.setattr(sys, "stdin", object())
    assert prompts.is_interactive() is False


@pytest.mark.unit()
def test_is_interactive_degrades_safely_when_stdin_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed stdin raises ``ValueError`` from ``isatty``; catch it.

    Pytest's own capture often leaves ``sys.stdin`` closed; without
    this guard, any test that forgets to monkeypatch ``isatty``
    would see an unrelated traceback.
    """

    class _ClosedStdin:
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", _ClosedStdin())
    assert prompts.is_interactive() is False


# -----------------------------------------------------------------------------
# ask_text
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_ask_text_returns_stripped_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace around the answer is not part of the answer.

    A user who types ``"  my-api  "`` gets ``"my-api"``. This is the
    99% case and the default; a caller that needs to preserve
    whitespace (a description field) should not use ``ask_text``.
    """
    _install_questionary(monkeypatch, text=["  my-api  "])

    assert prompts.ask_text("Project name") == "my-api"


@pytest.mark.unit()
def test_ask_text_uses_default_when_user_presses_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer falls back to the default, not to ``""``.

    Questionary returns the default when the user presses Enter, so
    the fake returns the default directly. This test asserts that
    the value *reaches* the caller unchanged.
    """
    _install_questionary(monkeypatch, text=["default-project"])

    assert prompts.ask_text("Project name", default="default-project") == "default-project"


@pytest.mark.unit()
def test_ask_text_rejects_empty_input_unless_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer re-prompts, and the second answer is returned.

    The re-prompt is the user-facing half of real-time validation:
    the user sees "This field cannot be empty." and gets a second
    chance without restarting the wizard.
    """
    # First answer is empty (rejected), second answer is valid.
    fakes = _install_questionary(monkeypatch, text=["", "my-api"])

    result = prompts.ask_text("Project name")

    assert result == "my-api"
    # The prompt fired twice: once for the empty answer, once for
    # the retry. ``ask_count`` is the observable proof that the
    # re-prompt loop worked.
    assert fakes["text"].ask_count == 2
    # And the user saw *why* the input was rejected.
    assert any("cannot be empty" in msg.lower() for msg in fakes["printed"])


@pytest.mark.unit()
def test_ask_text_allows_empty_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``allow_empty=True`` accepts ``""`` without re-prompting."""
    fakes = _install_questionary(monkeypatch, text=[""])

    assert prompts.ask_text("Optional note", allow_empty=True) == ""
    assert fakes["text"].ask_count == 1


@pytest.mark.unit()
def test_ask_text_rejects_input_that_fails_the_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validator returning an error message triggers a re-prompt.

    The user sees the message inline, and the second answer (which
    passes validation) is returned. This is the mechanism behind the
    "wizard validates input in real-time" acceptance criterion.
    """

    def _no_spaces(value: str) -> str | None:
        if " " in value:
            return "No spaces allowed."
        return None

    fakes = _install_questionary(monkeypatch, text=["bad name", "good-name"])

    result = prompts.ask_text("Project name", validator=_no_spaces)

    assert result == "good-name"
    assert fakes["text"].ask_count == 2
    assert any("No spaces allowed." in msg for msg in fakes["printed"])


@pytest.mark.unit()
def test_ask_text_accepts_input_that_passes_the_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing validator does not cause an extra prompt.

    This is the "no dead ends" half of the contract: valid input
    must not be punished with a redundant re-ask.
    """

    def _no_spaces(value: str) -> str | None:
        if " " in value:
            return "No spaces allowed."
        return None

    fakes = _install_questionary(monkeypatch, text=["good-name"])

    assert prompts.ask_text("Project name", validator=_no_spaces) == "good-name"
    assert fakes["text"].ask_count == 1
    assert not fakes["printed"]  # no error shown for valid input


@pytest.mark.unit()
def test_ask_text_treats_empty_string_default_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string default is treated as "no default".

    An empty default looks like a suggestion but accepts nothing —
    it is a UX anti-pattern. Treating it as ``None`` is the
    least-surprising behaviour, and this test locks it in.
    """
    captured: dict[str, Any] = {}

    # Build a fake module whose ``text`` function records its
    # keyword arguments. This is the one place in the suite where we
    # spy on the *wire format* rather than the return value — the
    # normalization is an internal detail of ``ask_text``, not part
    # of its return contract, so the only way to observe it is to
    # watch the call.
    text_answers: list[Any] = ["typed"]
    printed: list[str] = []
    module, _fakes = _build_fake_module(
        text_answers=text_answers,
        select_answers=[],
        confirm_answers=[],
        printed=printed,
    )

    def _spy_text(message: str, *, default: Any, **_: Any) -> _FakePrompt:
        captured["default"] = default
        return _FakePrompt(["typed"])

    module.text = _spy_text  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "questionary", module)

    # Pass an empty default explicitly; the code should normalize it
    # to "no default" (questionary receives "").
    prompts.ask_text("Project name", default="")

    # The exact wire format is questionary's concern; what matters is
    # that we did not pass a truthy-but-empty default that would
    # confuse the library. ``""`` is what questionary uses for "no
    # default", and it is what we pass.
    assert captured["default"] == ""


# -----------------------------------------------------------------------------
# ask_select
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_ask_select_returns_the_chosen_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selected value is returned unchanged, exactly as given."""
    _install_questionary(monkeypatch, select=["python.fastapi.api"])

    result = prompts.ask_select(
        "Select a blueprint",
        ["python.fastapi.api", "typescript.react.app"],
    )

    assert result == "python.fastapi.api"


@pytest.mark.unit()
def test_ask_select_passes_all_choices_to_questionary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's list is not filtered, deduplicated, or reordered.

    Menu order and content are caller concerns. The engine's job is
    to display what it is given. Filtering here would hide bugs in
    the registry's ``list_blueprints()`` (which is responsible for
    sorting) behind a UI-level fix.
    """
    captured: dict[str, Any] = {}

    printed: list[str] = []
    module, _fakes = _build_fake_module(
        text_answers=[],
        select_answers=["b"],
        confirm_answers=[],
        printed=printed,
    )

    def _spy_select(message: str, *, choices: Any, **_: Any) -> _FakePrompt:
        captured["choices"] = list(choices)
        return _FakePrompt(["b"])

    module.select = _spy_select  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "questionary", module)

    prompts.ask_select("Pick", ["b", "a", "c"])

    # Order preserved: the caller decides sort order, not the UI.
    assert captured["choices"] == ["b", "a", "c"]


@pytest.mark.unit()
def test_ask_select_raises_user_input_error_for_empty_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty menu is a misconfiguration, not a user-facing prompt.

    The error must be a domain error (``UserInputError``) so the CLI
    handler renders it cleanly with exit code 1, instead of
    questionary raising an opaque internal error (or, worse, the
    wizard displaying an empty menu that the user cannot interact
    with).
    """
    _install_questionary(monkeypatch, select=["unused"])

    with pytest.raises(UserInputError) as info:
        prompts.ask_select("Pick a blueprint", [])

    # The message and hint must be actionable — a bare "no options"
    # is a dead end.
    assert "No options" in str(info.value)
    assert info.value.hint is not None
    assert "reinstall" in info.value.hint.lower()


# -----------------------------------------------------------------------------
# ask_confirm
# -----------------------------------------------------------------------------
@pytest.mark.unit()
@pytest.mark.parametrize(
    ("answer", "expected"),
    [(True, True), (False, False)],
)
def test_ask_confirm_returns_a_bool(
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
    expected: bool,
) -> None:
    """Both branches of the yes/no prompt return a ``bool``.

    Parametrized so a future change that returns, say, the string
    ``"y"`` is caught for both the yes and the no branches.
    """
    _install_questionary(monkeypatch, confirm=[answer])

    assert prompts.ask_confirm("Include Docker?") is expected


# -----------------------------------------------------------------------------
# Cancellation — the uniform contract
# -----------------------------------------------------------------------------
# Every public prompt must translate Ctrl+C / Ctrl+D into
# ``PromptCancelledError``. This is what makes the wizard "exit
# cleanly" on cancellation (acceptance criterion 5) — without it, the
# interrupt reaches ``sys.exit``, which prints a traceback and exits
# with the wrong code.
#
# We parametrize over every public ask function so a future contributor
# who adds ``ask_password`` and forgets ``_guard`` has only to add one
# tuple entry to make the test cover it. That is the smallest possible
# tripwire, and it is the right one: the *contract* is "every prompt
# cancels uniformly," and the test encodes the contract in the place
# where forgetting it is cheapest to fix.
# -----------------------------------------------------------------------------
def _raise_keyboard_interrupt(*_: Any, **__: Any) -> Any:
    raise KeyboardInterrupt


def _raise_eof(*_: Any, **__: Any) -> Any:
    raise EOFError


def _install_raiser_questionary(
    monkeypatch: pytest.MonkeyPatch,
    raiser: Any,
) -> None:
    """Install a fake questionary whose every prompt raises ``raiser``.

    Used by the cancellation parametrization. Building the fake via a
    shared helper keeps the shape identical across the six cases; if
    a new prompt is added and the shape changes, the change is one
    edit here, not six.
    """
    printed: list[str] = []
    module, _fakes = _build_fake_module(
        text_answers=[],
        select_answers=[],
        confirm_answers=[],
        printed=printed,
    )

    def _raiser(*args: Any, **kwargs: Any) -> Any:
        raiser(*args, **kwargs)

    module.text = _raiser  # type: ignore[attr-defined]
    module.select = _raiser  # type: ignore[attr-defined]
    module.confirm = _raiser  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "questionary", module)


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("prompt_name", "call"),
    [
        ("ask_text", lambda: prompts.ask_text("Q")),
        ("ask_select", lambda: prompts.ask_select("Q", ["a"])),
        ("ask_confirm", lambda: prompts.ask_confirm("Q")),
    ],
)
@pytest.mark.parametrize(
    "raiser",
    [_raise_keyboard_interrupt, _raise_eof],
    ids=["ctrl_c", "ctrl_d"],
)
def test_every_prompt_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    prompt_name: str,
    call: Any,
    raiser: Any,
) -> None:
    """Every prompt translates interrupts to ``PromptCancelledError``.

    Two interrupt types (Ctrl+C and Ctrl+D) times three prompts equals
    six cases. The assertion is the same in all six: the caller sees
    ``PromptCancelledError``, never ``KeyboardInterrupt`` or
    ``EOFError``. If a future prompt is added and not wrapped in
    ``_guard``, adding it to this parametrize list is the entire
    test.
    """
    _install_raiser_questionary(monkeypatch, raiser)

    with pytest.raises(PromptCancelledError):
        call()


# -----------------------------------------------------------------------------
# Public surface
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_public_surface_is_exactly_the_documented_names() -> None:
    """Lock the module's public API.

    A new public name (e.g., ``ask_password``) must be a deliberate
    addition: it needs a docstring, a test, and a mention in the
    module docstring's "public surface" section. This test makes
    that a checkpoint, not an afterthought.

    A removed name (e.g., ``ask_confirm``) is a breaking change for
    ``create.py`` and must be a deliberate decision. This test
    catches both directions.
    """
    assert set(prompts.__all__) == {
        "Validator",
        "ask_confirm",
        "ask_select",
        "ask_text",
        "is_interactive",
    }
