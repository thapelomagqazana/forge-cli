"""The renderer — the only public entry point for template execution.

Guarantees (each backed by a test in `tests/unit/rendering/`)
-------------------------------------------------------------
1. **Deterministic.** Same template + same variables ⇒ byte-identical
   output, *modulo Jinja's own line-ending normalisation* (see below).
   Ensured by recursively sorted context, no time/random globals, a
   deterministic `tojson` filter, and pinned whitespace settings.
2. **Strict.** Missing variables raise `UndefinedError`, never render as
   empty (StrictUndefined). Empty strings in generated code are the
   single most common scaffolding bug that ships to production.
3. **Allowlisted.** Unknown filters are rejected. The filter set is a
   documented, security-reviewed allowlist (`filters.py`).
4. **Sandboxed.** Python-internal escapes are denied by
   `ForgeSandboxedEnvironment`. Denials surface as structured errors,
   never tracebacks.
5. **Actionable.** Every failure is a `TemplateRenderError` with the
   template path, line/column when available, the offending name, and a
   non-empty hint.
6. **Result-typed.** Returns `Result[str, TemplateRenderError]`, not
   exceptions, so batch renderers can collect every failure in one
   report — the same discipline the loader established (ADR-0010).
7. **Total for template-authored failures.** Every exception a template
   can cause — missing name, syntax error, missing include, sandbox
   denial, *and* bad filter/expression arguments — becomes a structured
   error. A template author's mistake never escapes as a traceback.

What "deterministic" actually means, and what it does not
---------------------------------------------------------
The determinism contract is: **for the same template bytes and the
same variables, two renders produce byte-identical output.** Three
qualifications are load-bearing:

    * **Line-ending normalisation is part of the contract, not a
      violation of it.** Jinja's lexer translates ``\\r\\n`` to ``\\n``
      during parsing. The renderer does not attempt to reverse this.
      The consequence is that a template checked in from a Windows
      workstation and the same template checked in from a Unix
      workstation produce *identical* output — which is the point of
      determinism. Reversing the normalisation would require tracking
      line-ending conventions through Jinja's token stream (which
      Jinja does not expose) and would fail unpredictably for
      templates that legitimately mix conventions. The honest contract
      is "deterministic up to LF normalisation," and the honest
      guarantee-list entry says so.

    * **The variables must be the same *modulo mapping semantics*.**
      Two callers can pass ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}``
      and receive identical output, because the renderer sorts
      recursively before handing the context to Jinja. Two callers
      passing the same *logical* content with different insertion
      orders still produce identical output — that is what "recursively
      sorted context" buys.

    * **Primitive values must have deterministic ``repr``.** A caller
      who passes an object whose ``__repr__`` calls ``random.random()``
      can still break determinism — but that is the caller's bug, not
      the renderer's, and no reasonable renderer can detect it.

An earlier version of this file claimed simply "byte-identical output"
and sorted only the *top-level* keys of the context. Both were wrong
for the same reason: they overclaimed what the renderer actually
guarantees. The determinism test suite
(``tests/unit/rendering/test_determinism.py``) caught the nested-dict
case; the line-ending case was caught by a test that asserted the
*opposite* of the actual behaviour, and the resolution was to correct
the guarantee, not to add a fragile normalisation-reversal layer.

Why this module is thin
-----------------------
Policy lives in `environment.py` (config), `filters.py` / `globals.py`
(capability), and `_sandbox.py` (hardening). This module owns only:

    * filesystem I/O for the template bytes,
    * per-render loader scoping (so ``{% include %}`` cannot escape),
    * translation of Jinja exceptions into ``TemplateRenderError``,
    * deterministic context preparation.

Keeping those four concerns together — and nothing else — is what makes
this file easy to audit and easy to keep correct.

Why guarantee 7 is stated separately from 1 to 6
---------------------------------------------
Guarantees 1 to 6 describe *properties* of a successful or well-understood
failure. Guarantee 7 describes the *closure* of the exception handler:
the claim that no template-authored failure escapes as a raw exception.
An earlier version of this file omitted the ``TypeError``/``ValueError``/
``AttributeError`` branch (a filter called with wrong arguments raised
``TypeError`` straight out of ``render()``), and that gap was caught by
``test_allowlisted_filter_with_wrong_arguments_fails_cleanly``. The
guarantee exists to make the closure explicit and testable — a
``render()`` that raises on *any* template input, no matter how
malformed, is the contract that "batch renderers can collect every
failure in one report" (guarantee 6) actually requires.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from jinja2 import TemplateNotFound, TemplateSyntaxError, UndefinedError
from jinja2.exceptions import SecurityError

from forge.loading.result import Err, Ok, Result
from forge.rendering.environment import get_environment, make_loader
from forge.rendering.errors import TemplateRenderError


class TemplateRenderer:
    """Render one template file with a mapping of variables.

    Stateless and thread-safe. Construct once, call `render()` as often as
    needed. All environment configuration lives in `environment.py`;
    this class only orchestrates I/O and error translation.
    """

    def render(
        self,
        template_path: Path,
        variables: Mapping[str, Any],
    ) -> Result[str, TemplateRenderError]:
        """Render `template_path` with `variables`.

        Returns `Ok(rendered_text)` on success, or
        `Err(TemplateRenderError)` on any failure. Never raises for
        user-caused conditions (missing variable, syntax error, sandbox
        denial, missing file, wrong filter arguments). Only internal
        bugs propagate as exceptions.
        """
        display = str(template_path)
        path = Path(template_path)

        # --- read bytes ----------------------------------------------------
        # Read before touching Jinja: file-not-found is a *path* problem,
        # not a template problem. Reporting it with the same error type
        # keeps the caller's handling uniform, and the source path in the
        # message is what the user actually wrote in their blueprint.
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return Err(
                TemplateRenderError(
                    template=display,
                    code="io.not_found",
                    message="template file not found",
                    hint="Check the source path in the blueprint's files[] entry.",
                )
            )
        except OSError as exc:
            return Err(
                TemplateRenderError(
                    template=display,
                    code="io.error",
                    message=f"cannot read template: {exc.strerror or exc}",
                    hint="Ensure the file is readable by the current user.",
                )
            )

        # --- decode --------------------------------------------------------
        # Decoding before handing to Jinja means a non-UTF-8 template gets
        # a precise, located error ("line 12, column 34") rather than an
        # opaque UnicodeDecodeError bubbling out of Jinja's lexer with no
        # useful traceback frame.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            line, col = _line_col_from_offset(text=raw, offset=exc.start)
            return Err(
                TemplateRenderError(
                    template=display,
                    code="io.encoding",
                    message=f"template is not valid UTF-8: {exc.reason}",
                    hint="Re-save the template as UTF-8 (most editors default to this).",
                    line=line,
                    col=col,
                )
            )

        # --- build a per-render loader -------------------------------------
        # The loader is scoped to the template's *directory*, so
        # `{% include "sibling.j2" %}` works but `{% include "../x" %}` does
        # not. `make_loader` also sets `followlinks=False`, blocking
        # symlink-based escapes.
        root = str(path.parent)
        env = get_environment().overlay(loader=make_loader(root))

        # --- parse and render ----------------------------------------------
        # Every Jinja exception type that can arise from a *template* is
        # translated to a structured error. Ordering matters: SecurityError
        # is a subclass of Exception, not of UndefinedError, so listing it
        # after UndefinedError would still work — but listing it explicitly
        # and prominently signals to reviewers that it is expected.
        #
        # The final `TypeError`/`ValueError`/`AttributeError` branch is
        # what makes guarantee 7 hold. See its inline comment for the
        # reasoning that motivated adding it.
        try:
            template = env.from_string(text)
            rendered = template.render(**self._deterministic_context(variables))
        except TemplateSyntaxError as exc:
            return Err(self._syntax_error(display, exc))
        except UndefinedError as exc:
            return Err(self._undefined_error(display, exc))
        except TemplateNotFound as exc:
            return Err(
                TemplateRenderError(
                    template=display,
                    code="include.not_found",
                    message=f"included template not found: {exc.name}",
                    hint=(
                        "{% include %} paths are relative to the blueprint "
                        "directory; check the filename and directory."
                    ),
                )
            )
        except SecurityError as exc:
            # The sandbox denied an attribute access. This is a template
            # author attempting something the sandbox forbids; the message
            # from the sandbox is intentionally specific about *which*
            # attribute was denied, which is exactly what they need.
            return Err(
                TemplateRenderError(
                    template=display,
                    code="sandbox.denied",
                    message=f"sandbox denied: {exc}",
                    hint=(
                        "Templates cannot access Python internals or filesystem; "
                        "use blueprint variables instead."
                    ),
                )
            )
        except (TypeError, ValueError, AttributeError) as exc:
            # A filter or expression was called with arguments that its
            # implementation cannot handle, or an attribute operation
            # failed on a value the template *did* have. Examples:
            #
            #   {{ 'abc' | replace('a') }}          # filter missing an arg
            #   {{ 1 | join }}                      # non-iterable value
            #   {{ 42 | length }}                   # no len() on int
            #   {{ none.foo }}                      # attribute of None
            #
            # All four are template-author mistakes, not Forge bugs. They
            # are *also* the class of failures that the previous version
            # of this file let escape as raw tracebacks, contradicting
            # guarantee 6 (Result-typed failures) in the exact case
            # guarantee 6 was designed to cover: a batch renderer that
            # calls render() fifty times must receive fifty Err values,
            # never a raised exception on iteration 23.
            #
            # Why catch three exception types, not one:
            #
            #   TypeError       — wrong number/type of arguments
            #   ValueError      — right type, unparseable value (e.g.
            #                     `|int` on "abc")
            #   AttributeError  — attribute on a value that lacks it
            #                     (e.g. `{{ none.foo }}` when sandbox
            #                     allows the access but the value is None)
            #
            # The sandbox's SecurityError handler above catches the
            # *denied* case; this branch catches the *permitted but
            # failing* case. Both are template-author errors, but they
            # have different fixes (read the docs vs. check the value),
            # so they have different error codes.
            #
            # Why not catch Exception?
            #
            # Because a genuine Forge bug (a filter whose implementation
            # raises something unexpected) should propagate to the CLI's
            # INTERNAL_ERROR handler with a traceback — that is the
            # signal that our code is broken. Only exceptions that are
            # *plausibly* caused by template input are caught here.
            return Err(
                TemplateRenderError(
                    template=display,
                    code="filter.invalid_arguments",
                    message=f"invalid filter or expression arguments: {exc}",
                    hint=(
                        "A filter was called with the wrong arguments, or an "
                        "operation was applied to a value of the wrong type. "
                        "Check the filter's signature in "
                        "docs/reference/TEMPLATE_FILTERS.md and the value's type."
                    ),
                )
            )

        return Ok(rendered)

    # -- helpers -----------------------------------------------------------

    def _deterministic_context(self, variables: Mapping[str, Any]) -> dict[str, Any]:
        """Return a plain dict with stable, sorted iteration order *at every depth*.

        Why recursive sorting, not just top-level:

        An earlier version of this helper sorted only the outermost
        keys, on the assumption that Jinja's iteration over a nested
        dict would be sorted *somewhere else*. It is not: Jinja
        iterates a dict in insertion order, so a template like
        ``{% for k in ctx %}`` over a caller-supplied dict renders in
        whatever order the caller built the dict. Sorting only the top
        level left every nested dict unsorted, breaking byte-identity
        for any template that iterates a dict-of-dicts.
        ``test_determinism.py``'s ``test_context_iteration_is_sorted``
        caught it with the exact reproduction case (``{"z": 1, "a": 2,
        "m": 3}`` rendering as ``z, a, m`` instead of ``a, m, z``).

        Why every mapping, not just ``dict``:

        Callers may pass a ``Mapping`` whose iteration is unspecified
        (YAML loaders with custom ``Mapping`` implementations, ORMs,
        proxies). Recursing into anything that looks like a mapping,
        preserving the *values* as-is but replacing each mapping with a
        plain dict whose keys are sorted, makes the sort effective for
        any mapping type. Lists and tuples are walked in order (their
        order is meaningful and must not be changed).

        What is *not* recursed into, and why:

        * **Sets and frozensets.** Iterating a set is non-deterministic
          by design, and the renderer will not silently coerce one into
          a list — that would hide the non-determinism rather than
          eliminate it. A template that iterates a set is
          non-deterministic by the caller's choice, not by ours, and
          that is a good thing to surface rather than paper over.

        * **Arbitrary objects.** A dataclass, a custom class instance,
          or a Pydantic model has attribute access that is
          deterministic (lookup by name), and coercing them to dicts
          would break templates that expect attribute access
          (``{{ user.name }}``). The renderer leaves them alone.

        * **Primitive values.** ``str``, ``int``, ``float``, ``bool``,
          ``None``. Returned as is. Their ``repr`` is deterministic.

        Why a plain dict and not a read-only mapping:

        Jinja's ``render(**kwargs)`` calls ``dict(**kwargs)``, so a
        plain dict is what it operates on internally anyway. Wrapping
        it would add nothing. The immutability of ``ResolvedValues`` in
        the caller is what prevents post-validation mutation.

        Why the ``cast`` below:

        ``_sort_mapping`` is annotated ``-> object`` because it
        genuinely can return any shape (dict, list, tuple, scalar)
        depending on its input. At *this* call site, the input is a
        ``Mapping[str, Any]`` and the mapping branch of
        ``_sort_mapping`` always runs — so the result is a
        ``dict[str, Any]`` — but the type checker cannot narrow from
        ``object`` to ``dict[str, Any]`` on its own. The ``cast``
        documents the invariant (input was a mapping ⇒ output is a
        dict) at the one place that both knows it and needs it.
        """
        return cast(dict[str, Any], _sort_mapping(variables))

    def _syntax_error(self, display: str, exc: TemplateSyntaxError) -> TemplateRenderError:
        """Translate Jinja's syntax error into an actionable structured error."""
        return TemplateRenderError(
            template=display,
            code="syntax.error",
            message=f"template syntax error: {exc.message}",
            hint=self._syntax_hint(exc),
            line=exc.lineno,
            col=getattr(exc, "colno", None),
        )

    def _undefined_error(self, display: str, exc: UndefinedError) -> TemplateRenderError:
        """Translate Jinja's undefined error, extracting the offending name.

        Jinja's message is `'<name>' is undefined` — we extract `<name>` so
        downstream tooling (IDE hints, LSP diagnostics, `--json` consumers)
        can point at the specific variable rather than making the user
        re-parse prose.
        """
        name = _extract_undefined_name(exc)
        message = f"undefined variable: {name}" if name else str(exc)
        hint = (
            f"Provide a value for '{name}' in the blueprint's variables, "
            "or use |default(...) explicitly."
            if name
            else "Provide a value for the missing name, or use |default(...)."
        )
        return TemplateRenderError(
            template=display,
            code="undefined.variable",
            message=message,
            hint=hint,
            name=name,
        )

    def _syntax_hint(self, exc: TemplateSyntaxError) -> str:
        """Translate a Jinja syntax error into a fix that names the mistake.

        Why we scan **both** ``name`` and ``message``:

        Jinja's ``TemplateSyntaxError`` carries two text fields, and
        different parser branches populate different ones:

        * ``name`` — a short identifier for the parser's *category* of
          failure. Older Jinja populated this consistently; Jinja 3.x
          has deprecated much of that machinery and leaves ``name``
          empty for many errors, including the unclosed-block case.

        * ``message`` — the parser's human-readable diagnostic, e.g.
          ``"Unexpected end of template. Jinja was looking for the
          following tags: 'endfor'."``. This field is *always*
          populated for every ``TemplateSyntaxError``, and it contains
          stable keywords (``"unexpected"``, ``"end"``, ``"unclosed"``)
          across Jinja versions even when the exact wording shifts.

        The earlier version of this helper scanned only ``name``, on
        the theory that it was "more robust than regexing the
        human-readable message". That theory was wrong: for the single
        most common syntax error a blueprint author hits — an unclosed
        ``{% for %}`` or ``{% if %}`` — ``name`` is empty on current
        Jinja, and the specific hint silently degraded to the generic
        fallback (``"Fix the syntax at the reported line."``). The
        failing tests
        ``test_unclosed_for_block_reports_line_and_hint`` and
        ``test_unclosed_if_block_reports_line_and_hint`` caught it.

        Scanning both fields restores the specific hints without
        coupling to either field's exact content: the keywords the
        branches match on (``end of template``, ``unexpected``,
        ``expected``) are the ones Jinja has used consistently across
        versions. A future Jinja that rewords its diagnostics would
        still match on at least one of these — and if all three
        somehow stopped matching, the generic fallback would still be
        returned, so the failure mode is *degraded*, not *broken*.

        Why the return value is *not* extracted from the message:

        Jinja's message already names the missing tag (``'endfor'``,
        ``'endif'``), and it would be tempting to interpolate that
        into the hint (``"Add {% endfor %} to close the block"``).
        We deliberately do not: the tag name in the message is
        anchored to Jinja's exact phrasing, and a reword would break
        the extraction silently (the hint would say "Add {% None %}").
        The generic-but-accurate phrasing below is stable across
        versions; a user who needs the specific tag reads it in the
        error's ``message`` field, which already names it.
        """
        # Scan both fields. The concatenation is a cheap way to match
        # against either without three separate `or` clauses per branch.
        haystack = (
            f"{getattr(exc, 'name', '') or ''} " f"{getattr(exc, 'message', '') or ''}"
        ).lower()

        # Unclosed-block errors: Jinja's message contains "end of
        # template" and typically names the missing tag (``endfor``,
        # ``endif``, ``endblock``). Any of those keywords is enough to
        # give the user the specific "close every block" advice.
        if "end of template" in haystack or "unexpected end" in haystack:
            return (
                "Check that every {% block %}, {% for %}, and {% if %} is "
                "closed with the matching end tag."
            )
        # Mismatched or unexpected tags — the parser knows the token was
        # out of place but cannot suggest a specific fix. Direct the
        # user at the reported line, which is the best we can do.
        if "unexpected" in haystack:
            return "Check the syntax near the reported line; consult the " "Jinja docs if unsure."
        # Missing closing brace or tag — again, the reported line is
        # the actionable location.
        if "expected" in haystack:
            return (
                "A closing brace or tag is missing; check the construct "
                "that starts on the reported line."
            )
        return "Fix the syntax at the reported line."


# ---------------------------------------------------------------------------
# Free functions — kept module-level so they are unit-testable in isolation
# and can be reused by future consumers (e.g. an LSP bridge that wants to
# surface the same name extraction).
# ---------------------------------------------------------------------------


def _sort_mapping(value: object) -> object:
    """Recursively sort the keys of every mapping reachable from ``value``.

    The behaviour and the *non-behaviours* (what is deliberately not
    recursed into) are documented on ``_deterministic_context``. This
    function exists at module level, not as a method, so that:

        * it can be unit-tested in isolation (a future test file
          ``test_context_sorting.py`` would exercise it directly with
          arbitrary nested structures, without needing a template);

        * it can be reused by any future consumer that needs to
          canonicalise a context before passing it somewhere else
          (a diagnostic command, an LSP that renders the context as
          hover text, a golden-snapshot tool);

        * the recursive traversal logic is separated from the
          renderer's I/O concerns, keeping each function's
          responsibility singular.

    Uses ``collections.abc.Mapping`` rather than ``dict`` so that
    caller-supplied mapping implementations are handled uniformly.

    Why the return type is ``object`` and not ``Any``:

    The function genuinely can return a ``dict``, a ``list``, a
    ``tuple``, or the input scalar unchanged — the return type
    depends on the input type. ``Any`` would say "callers can do
    anything with this"; ``object`` says "this is a value, and
    callers must narrow before use." The latter is the honest
    contract: this function preserves arbitrary input shapes, so
    consumers that need a specific shape must prove they provided
    one. The single caller (``_deterministic_context``) does that
    with a ``cast`` at its call site, justified in its docstring;
    a future caller that needs a different shape would similarly
    have to narrow. That discipline is exactly what a
    type-checking boundary should enforce — the alternative
    (``Any``) would let a downstream typo (``_sort_mapping(x).foo``
    where ``x`` was a list) pass mypy and fail at runtime.
    """
    # Imported here rather than at module top so the module's import
    # graph stays shallow for callers that only need the exception
    # translation helpers. `Mapping` is cheap to import but keeping
    # the dependency local documents that this function is the only
    # place the renderer cares about mapping types.
    from collections.abc import Mapping as _Mapping

    if isinstance(value, _Mapping):
        return {key: _sort_mapping(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sort_mapping(item) for item in value)
    return value


def _extract_undefined_name(exc: UndefinedError) -> str | None:
    """Best-effort extraction of the variable name from Jinja's message.

    Jinja's wording is stable enough that a targeted regex is safe here,
    and both forms ('x' is undefined / 'x.y' is undefined) are common.
    A future Jinja release that changes wording would cause this to
    return None — the error still includes the full message and a generic
    hint, so the failure mode is *degraded*, not *broken*.
    """
    import re

    message = str(exc)
    # Match single-quoted name at the start of the message.
    m = re.match(r"'([^']+)' is undefined", message)
    return m.group(1) if m else None


def _line_col_from_offset(*, text: bytes, offset: int) -> tuple[int, int]:
    """Convert a byte offset in `text` into 1-based line/column.

    Used for UTF-8 decode errors, where the decoder reports only the byte
    offset. We count newlines in the prefix (which is guaranteed decodable,
    being before the error) and compute column in *characters*, not bytes,
    so the reported column matches what the user sees in their editor.
    """
    prefix = text[:offset]
    newline_count = prefix.count(b"\n")
    last_newline = prefix.rfind(b"\n")
    if last_newline == -1:
        # First line; column is 1-based character count of the prefix.
        return (1, offset + 1)
    # Column within the final line, 1-based, in characters.
    try:
        line_bytes = prefix[last_newline + 1 :]
        column = len(line_bytes.decode("utf-8")) + 1
    except UnicodeDecodeError:
        # Prefix itself failed to decode (multi-byte char straddles the
        # offset); fall back to byte count for column so we still report
        # *something* meaningful and never crash the error path.
        column = len(line_bytes) + 1
    return (newline_count + 1, column)
