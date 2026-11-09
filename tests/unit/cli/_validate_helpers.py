"""Shared helpers for the `forge validate` unit tests.

Why this file exists
--------------------
`validate.validate` raises `typer.Exit` on every exit path — the
mechanism Typer documents for exit codes under `standalone_mode=False`
(see the module docstring's "Exit-code mechanism" section). Every
test that asserts on the exit code therefore needs to:

    1. Call `validate(...)`.
    2. Catch the `typer.Exit` it raises.
    3. Compare `exc.exit_code` against the expected `ExitCode`.

That three-line pattern appears in every test file that invokes
`validate` (paths, exit codes, all-errors, JSON). Repeating it at
every call site would bury the assertions in ceremony; keeping it in
one helper means the test bodies read as `assert _exit_code(...) == X`.

A note on `pytest.raises`
-------------------------
An alternative pattern is `with pytest.raises(typer.Exit): ...` at
each call site. That is the *right* pattern when the test's subject
is "does this raise?" — e.g. `test_validate_paths.py`'s
`test_loader_exception_under_debug_is_reraised`. It is the *wrong*
pattern when the test's subject is "what code does this exit with?",
because the raise is an implementation detail of how the code is
delivered, not the contract under test. This helper unwraps the
implementation detail so the test can assert on the contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import typer

_T = TypeVar("_T")


def _exit_code(call: Callable[[], object]) -> int:
    """Return the integer exit code raised by `call`, unwrapping `typer.Exit`.

    Parameters
    ----------
    call
        A zero-argument callable that invokes `validate` (or any other
        command that raises `typer.Exit` on every exit path). A lambda
        is the normal form:

            _exit_code(lambda: validate(path=manifest, json=False))

    Returns
    -------
    int
        The integer value of `exc.exit_code` from the raised
        `typer.Exit`.

    Raises
    ------
    AssertionError
        If `call` returns without raising. Every exit path in
        `validate` raises; a return means the mechanism has regressed
        and this helper should fail loudly rather than masking the
        regression with a fake success code. The assertion message
        names the invariant, so the failure points at the fix.
    """
    try:
        call()
    except typer.Exit as exc:
        return int(exc.exit_code)
    raise AssertionError(
        "`validate(...)` returned without raising `typer.Exit`. Every "
        "exit path must raise; a return means the exit-code mechanism "
        "regressed (see `validate.py`'s `_coerce_bool` docstring and "
        "the module's 'Exit-code mechanism' section)."
    )
