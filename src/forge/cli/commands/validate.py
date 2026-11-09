"""``forge validate`` — validate a blueprint definition.

This command is the primary authoring tool for blueprint creators: it
runs the same loader pipeline that ``forge create`` uses, without
generating a project, and prints every problem it finds in one pass.

Why this command reuses the loader verbatim
-------------------------------------------
The temptation when writing a "checker" command is to assemble a
lightweight validation path just for it — "we only need schema
validation, not the filesystem checks" and so on. That temptation is
wrong, and not just in the abstract: if ``forge validate`` accepted
documents that ``forge create`` rejects, blueprint authors would ship
files that fail at generation time. The two commands must agree
exactly, which means the checker must call the same
``BlueprintLoader.load()`` that ``create`` calls — same schema, same
models, same filesystem checks, same error rendering.

The one deliberate difference is *reporting*: ``create`` aborts on the
first phase it can (its ``Result`` short-circuits so the user is not
waiting on template lookups when their YAML is broken), whereas
``validate`` shows *everything the loader surfaced*, which is already
every violation from every phase it reached. Users see one report,
not a ping-pong loop.

Command structure
-----------------
The module exposes a sub-Typer named ``validate`` whose *callback*
carries the command's arguments. This is the "single-command app"
shape that ``forge.cli.registry`` explicitly supports (see
``_is_single_command_app`` in ``registry.py``): the registry detects
the callback, registers the callback *function* as a leaf command on
the root app, and produces the invocation shape users expect —
``forge validate <path>``.

Exit-code mechanism: return, not raise
--------------------------------------
The command declares ``-> int`` and returns its exit code rather than
raising ``typer.Exit``. This is a deliberate choice with a specific
justification:

* Under ``standalone_mode=False``, Typer *returns* the callback's
  return value from ``app(...)``. That has been the documented
  behaviour on every Typer version since the flag existed. Reading
  the return value in ``main()`` is therefore portable.
* ``typer.Exit`` propagation under ``standalone_mode=False`` is *not*
  documented as stable, and in some Typer versions it is swallowed —
  ``app(...)`` returns the callback's value (``None``, for a command
  declared ``-> None``) rather than raising. That silently loses the
  exit code the command chose.
* Returning the exit code sidesteps the version-dependence entirely.
  ``main()`` reads the return value; if it is an integer, that is the
  exit code. No exception machinery, no version-specific behaviour.

The module still imports ``typer`` for the sub-Typer constructor and
for ``typer.Argument`` / ``typer.Option``, but the command body no
longer raises ``typer.Exit``. ``--debug`` behaviour is unchanged: on
an escaping exception, the command re-raises, and ``main()``'s
``handle_exception`` (with ``debug=True``) surfaces the traceback.

Why not ``@app.command(name="")``?
----------------------------------
That idiom makes the sub-Typer a *group* with a default command, and
``add_typer`` (which the registry uses for multi-command apps) nests
it. The result is ``forge validate <path>`` failing with
``UsageError: No such command '<path>'``, because Typer treats the
path as the next subcommand token. The callback shape avoids this by
construction: there is no group to nest, only a command function to
register.

Why the callback, not a plain command?
--------------------------------------
The registry distinguishes single-command apps from multi-command
apps by checking ``registered_callback is not None`` and
``len(registered_commands) == 0``. A callback-only app is
unambiguously a single command; an app with one ``@app.command()`` is
ambiguous (is it a group with one command, or a leaf?). Choosing the
callback shape removes the ambiguity at the source and makes the
registry's detection correct by construction.

Exit codes
----------
The command obeys the project's stable exit-code contract (ADR-0013,
``docs/reference/exit-codes.md``). Its exit code is **total** — every
documented path produces one of three values *from the loader's
perspective*, plus exit 5 for genuine internal failures, matching the
convention every other command follows:

* ``0`` — validation succeeded.
* ``1`` — the blueprint is invalid. The loader returned
  ``Err([...])``; every reported violation is rendered.
* ``2`` — usage error emitted by Typer *before* the command body
  runs (missing ``PATH``, path does not exist, unreadable path), or
  by the command body when a directory was passed without a
  ``blueprint.yaml`` (a caller-side mistake, not a blueprint problem).
* ``5`` — an *unexpected* exception escaped the loader. From the
  user's perspective "the loader crashed on my file" and "my file is
  invalid" both mean "my file did not validate", but they have
  different remedies: a crash means *the tool* has a bug and the
  user should report it; an invalid blueprint means *the blueprint*
  needs fixing. Collapsing them into a single code would lose that
  distinction and violate ADR-0013, which assigns ``5`` specifically
  to "internal error, report it". So an escaping loader exception
  returns ``5`` with a message pointing at ``--debug`` and the issue
  tracker.

An earlier draft of this command collapsed the loader-crash case into
``1`` to keep the contract "narrower". That was rejected: ``validate``
must be usable in CI, and a CI job that branches on ``$?`` needs to
tell "your blueprint has a problem" from "Forge has a problem". The
whole point of ADR-0013's exit ``5`` is to make Forge bugs visible.
The loader is designed to be total, so a loader exception is, by
construction, a Forge bug — and Forge bugs are ``5``.

Under ``--debug``, an escaping exception is re-raised rather than
returned, so a contributor diagnosing the bug gets the full
traceback. The two audiences (users and contributors) get what each
of them needs from the same code path.

Path resolution
---------------
The command accepts either a directory (with a ``blueprint.yaml``
inside) or a file. Resolution happens here, not inside the loader,
because "which file does the user mean" is a CLI concern. The loader
only loads *a file*. This keeps the loader's contract narrow and makes
its tests trivial (a temp ``blueprint.yaml``), while the CLI's
flexibility (accept a folder, accept a bare file) is captured where it
belongs.

Output modes
------------
The command has two output modes, dispatched by ``_render_dispatch``
after a successful load. Both modes carry the *same* exit code; the
mode changes presentation, not verdict.

**Human mode** (default): one error per line, using
``ValidationError.format()``, which the loader guarantees renders
``source: path: message — hint: fix`` and, when the parser could
locate the failure (YAML parse errors), ``source:line:col: path:
message — hint: fix``. A leading summary line
(``N validation error(s):``) orients the user before the list, and a
trailing success line (``OK: <id> v<version>``) tells them the
command did something when there are no errors.

**JSON mode** (``--json``): a versioned envelope built as a
``ValidateOutput`` DTO (see ``forge.cli.schemas.validate_output``)
and serialised by ``render_validate_json``. The DTO's field set,
nullability, and version literal are enforced at construction, so
the serialised shape cannot drift from the documented contract
(``docs/reference/CLI_VALIDATE_OUTPUT.md``). JSON output goes to
stdout only; nothing in either branch writes to stderr when
``--json`` is active, because mixing human-readable prose into a
machine-readable stream breaks consumers (see ``test_validate_json``).

Both renderers live in ``forge.cli.renderers`` and are kept pure (no
I/O, no sorting, no ANSI); the command's only job at this stage is to
pick one and forward its result. The two-renderer split is ADR-0012.

Why ``--json`` is coerced to a real ``bool`` before dispatch
------------------------------------------------------------
``@app.callback()`` returns the decorated function *unchanged*, so a
direct caller — a unit test, a script importing ``validate``, a
future embedding of the CLI — invokes the Python signature, not the
Typer dispatch. The default for a ``typer.Option(False, "--json")``
parameter is an ``OptionInfo`` instance, not ``False``. Typer unpacks
it during dispatch; a direct Python call does not.

``OptionInfo`` is a non-empty object, so ``if as_json:`` is truthy
even when the flag was omitted. Left alone, a direct caller that says
nothing about the mode gets JSON mode instead of human mode — a
behaviour-change-on-import bug that is invisible in review and
impossible to diagnose from a downstream stack trace.

``_coerce_bool`` makes the unwrapping explicit and testable, and its
docstring records why it exists so a future maintainer does not
delete it on the theory that "Typer always passes a bool." Typer
*does* when the flag comes through dispatch; direct callers do not,
and the guard is for them.

What this command deliberately does not do
------------------------------------------
* **Check blueprint *identity* against the registry.** A blueprint
  with ``id: python.fastapi.api`` that shadows an official one is
  *valid*; whether it should be allowed is a discovery concern
  (ADR-0009), not a validation concern. ``forge validate`` answers
  "is this file structurally sound?", not "should this file be
  installed?".

* **Verify the blueprint's verification contract.** Each check in the
  blueprint's ``verification[]`` describes what the *generated
  project* must satisfy. Running those checks against a
  not-yet-generated project is meaningless. This command validates
  the *blueprint*, not the *project*.

* **Render templates.** Loading does not render; a template that
  exists but contains Jinja syntax errors is a *create*-time failure,
  not a *validate*-time one. Rendering is expensive and belongs to
  ``forge create``; validation is fast and belongs here. Blueprint
  authors get template syntax errors when they run ``forge create``
  against a test project, which is the natural workflow.

Deferred improvements
---------------------
* **Thread ``ctx.obj`` through the registry.** The current
  ``_debug_requested()`` helper scans ``sys.argv`` because the
  registry does not pass ``typer.Context.obj`` to single-command
  callbacks. When the registry grows support for it (or when a
  second root-level flag needs the same treatment), ``_debug_requested``
  should be replaced with a read of ``ctx.obj["debug"]``. Recorded
  here so a contributor replacing the argv scan has a clear
  destination. (``--json`` is a *command-level* flag, not a root
  one, so it does not participate in this deferred work: Typer
  threads it through the callback signature like any other
  ``typer.Option``.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from typer.models import OptionInfo

from forge.cli import ui
from forge.core.exceptions import ExitCode
from forge.loading import BlueprintLoader
from forge.loading.errors import ValidationError
from forge.loading.result import Err, Ok
from forge.models import Blueprint

# -----------------------------------------------------------------------------
# Command module contract
# -----------------------------------------------------------------------------
# Every command module exposes a module-level ``app`` attribute of type
# ``typer.Typer``. The registry (``forge.cli.registry``) discovers this
# attribute and, for a single-command app, registers the callback as a
# leaf command on the root CLI under ``name``.
app = typer.Typer(
    name="validate",
    help="Validate a blueprint definition.",
)


# -----------------------------------------------------------------------------
# Option coercion
# -----------------------------------------------------------------------------
def _coerce_bool(value: object, *, default: bool) -> bool:
    """Return a real ``bool``, unwrapping a Typer ``OptionInfo`` if present.

    The reason this exists (and the reason it must not be removed):

    * ``@app.callback()`` returns the decorated function unchanged, so
      a *direct* caller — a unit test, an embedding script, a future
      non-CLI entry point — is calling the Python signature, not the
      CLI. Typer unpacks ``OptionInfo`` during dispatch; a direct
      caller does not.
    * ``OptionInfo`` is a non-empty object, so ``if value:`` is truthy
      even when the flag was omitted. Any branch that treats the
      parameter as a boolean (like ``_render_dispatch``'s
      ``if as_json:``) silently takes the "flag was set" path for a
      caller who said nothing about it — the classic
      behaviour-change-on-import bug.

    The unwrap is conservative: ``OptionInfo`` exposes ``.default``,
    which is the value Typer would have passed through dispatch. Any
    other value falls through to ``bool(value)``, which is correct
    for real ``bool`` parameters and is what Typer produces in normal
    operation.

    ``default`` is passed explicitly rather than baked into the
    helper so the coercion is honest about the *caller's* intent: a
    caller with a default of ``False`` and one with a default of
    ``True`` both get the right answer, and the helper does not
    invent a third policy that neither of them asked for.
    """
    if isinstance(value, OptionInfo):
        return bool(value.default)
    return bool(value) if value is not None else default


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
@app.callback()
def validate(
    path: Path = typer.Argument(
        ...,
        metavar="PATH",
        help=(
            "Path to a blueprint directory (containing blueprint.yaml) "
            "or to a blueprint.yaml file."
        ),
        # ``exists=True`` makes Typer reject missing paths with exit
        # code 2 *before* the command body runs. That is correct: a
        # missing path is a usage error (the caller asked for
        # something that is not there), not a blueprint error (the
        # blueprint is malformed). Keeping the two categories distinct
        # lets scripts branch on ``$?`` unambiguously.
        exists=True,
        # Accept both a directory and a file. The body resolves which
        # one was passed; a directory is treated as "the blueprint
        # folder", a file is treated as "the blueprint manifest".
        file_okay=True,
        dir_okay=True,
        # Reject unreadable paths at the CLI boundary with a usage
        # error, rather than letting the loader fail with a
        # blueprint-shaped error for a permissions problem. Two
        # categories, two messages, two exit codes.
        readable=True,
        # Resolve to an absolute path so the loader's ``source`` labels
        # are stable regardless of the caller's working directory and
        # so the rendered output does not depend on ``cwd``.
        resolve_path=True,
    ),
    json: bool = typer.Option(
        False,
        "--json",
        help="Emit a versioned JSON envelope instead of human-readable text.",
        # No short flag: ``-j`` is reserved for a hypothetical future
        # ``--jobs`` (parallel validation of multiple blueprints).
        # Giving ``--json`` a short alias now would force a rename
        # later, when the alias is more expensive to change than the
        # convenience is worth.
    ),
) -> int:
    """Validate a blueprint without generating a project.

    Returns the process exit code (see the module docstring's
    "Exit-code mechanism: return, not raise" section for why the exit
    code is a return value rather than a raised ``typer.Exit``).
    """
    # ------------------------------------------------------------------
    # 1. Resolve the user's path to a blueprint manifest.
    #
    # A folder is the *normal* case (blueprint authors keep the
    # manifest at the root of their blueprint directory), but a direct
    # file path is convenient for editors and CI. Accept both; reject
    # neither silently.
    # ------------------------------------------------------------------
    manifest = _resolve_manifest(path)
    if manifest is None:
        # A directory was passed but contains no ``blueprint.yaml``.
        # This is a *usage* error: the caller pointed at something
        # that is not a blueprint directory. Return code 2 (Typer's
        # own usage-error code) so scripts cannot distinguish "forgot
        # PATH" from "PATH is not a blueprint folder" — both mean
        # "you called me wrong", not "your blueprint is invalid".
        #
        # The message goes through the *human* stream unconditionally:
        # even in ``--json`` mode, the envelope machinery never runs
        # because the loader was never called. Emitting the human
        # message here is correct because this is a *usage* error,
        # not a validation result, and a usage error in JSON mode is
        # a caller mistake worth surfacing on stderr regardless.
        ui.error(
            f"No blueprint.yaml found in directory: {path}\n"
            f"  hint: A blueprint directory must contain a "
            f"'blueprint.yaml' at its root."
        )
        return int(ExitCode.BLUEPRINT_ERROR)  # 2

    # ------------------------------------------------------------------
    # 2. Run the loader.
    #
    # This is the *same* loader that ``forge create`` uses, so the two
    # commands cannot disagree about what constitutes a valid
    # blueprint. The loader's Result carries every problem it found
    # across all four phases (parse, schema, model, filesystem) in one
    # list; we do not need to iterate anything ourselves.
    #
    # The loader is designed to be total: every *expected* failure
    # (parse error, schema violation, model parity error, missing
    # template, unreadable file) is returned as ``Err([...])``. But
    # the loader runs inside a real process — a missing bundled
    # resource, an OS-level permission error, or a bug in a phase
    # could raise. Per ADR-0013, an *unexpected* exception is a Forge
    # bug, not a user error, and it must surface as exit 5 so CI
    # jobs can distinguish "your blueprint has a problem" from "Forge
    # has a problem". Collapsing both into exit 1 would lose that
    # distinction and hide real bugs behind a "blueprint invalid"
    # message that the user cannot fix.
    #
    # The report on this path is always human-readable, even in
    # ``--json`` mode: a loader crash is a Forge bug, and the
    # JSON envelope (`ValidateOutput`) is shaped for *blueprint
    # validation results*, not for tool failures. Emitting a
    # `{"valid": false, "errors": [...]}` envelope for "the loader
    # crashed" would force every consumer to distinguish "the
    # blueprint was rejected" from "the tool blew up" by inspecting
    # `errors[0].code` — which is exactly the sort of mixed-message
    # stream the ADR-0012 renderer split exists to avoid. The
    # correct signal for "tool failed" is exit 5 and stderr prose.
    #
    # Under ``--debug``, we do *not* catch: a contributor debugging
    # the loader wants the traceback, and swallowing it would hide
    # the bug they are trying to fix. ``--debug`` re-raises, letting
    # ``main()`` surface the full traceback through
    # ``handle_exception(debug=True)``.
    # ------------------------------------------------------------------
    try:
        result = BlueprintLoader().load(manifest)
    except Exception as exc:  # — deliberate boundary
        if _debug_requested():
            # Debug mode: let ``main()`` render the traceback. The
            # user explicitly asked for it, and a contributor
            # diagnosing the loader needs the stack.
            raise
        # Convert the exception into a user-facing report, but
        # *preserve* the exit-5 contract: this is a Forge bug, not a
        # blueprint problem. The message names the exception class so
        # the user has a specific string to include in a bug report,
        # but the *hint* (and the exit code) tell them this is not
        # something they can fix in their blueprint.
        ui.print_error(f"Could not validate {path}: {type(exc).__name__}: {exc}")
        ui.print_hint(
            "This is an internal Forge error, not a problem with your "
            "blueprint. Run with --debug for a full traceback, or report "
            "this at https://github.com/thapelomagqazana/forge-cli/issues"
        )
        return int(ExitCode.INTERNAL_ERROR)  # 5

    # ------------------------------------------------------------------
    # 3. Render.
    #
    # Dispatch on the flag, then each renderer owns one output format
    # end to end (per ADR-0012). The dispatch helper is one line of
    # branching, which is the correct amount of branching: any more
    # belongs in a renderer; any less (inlining the branch here)
    # would tangle the renderer choice into the pipeline.
    #
    # ``json`` may be an ``OptionInfo`` when this callback is invoked
    # directly (tests, scripts, future embeddings). Coerce it to a
    # real ``bool`` before dispatching — see ``_coerce_bool``'s
    # docstring for the full reason. Passing the raw parameter through
    # would silently flip a caller who omitted the flag into JSON
    # mode, because ``OptionInfo`` is truthy.
    # ------------------------------------------------------------------
    as_json = _coerce_bool(json, default=False)
    return _render_dispatch(result, source=str(manifest), as_json=as_json)


# -----------------------------------------------------------------------------
# Path resolution
# -----------------------------------------------------------------------------
def _resolve_manifest(path: Path) -> Path | None:
    """Return the blueprint manifest file for ``path``, or ``None``.

    ``None`` is returned only for the "directory without a manifest"
    case — every other failure (missing file, unreadable file) has
    already been caught by Typer's argument validators, so a ``None``
    here unambiguously means "the user pointed at the wrong folder".

    Design note: returning ``None`` rather than raising makes the
    command body's control flow linear — a single ``if manifest is
    None`` branch that handles the *one* case Typer cannot handle for
    us. Exceptions would add a try/except around a call that has one
    failure mode, which is more ceremony than the case deserves.
    """
    if path.is_file():
        # A file path: assume it is the manifest. If it is some other
        # file, the loader will report a clear error ("invalid YAML"
        # or "blueprint root must be a mapping"), which is a better
        # message than "this is not blueprint.yaml" — the user
        # deliberately passed *this* file.
        return path

    # Directory path: the manifest lives at ``<path>/blueprint.yaml``.
    # We do not search deeper — a blueprint directory has exactly one
    # manifest, at its root, by contract. Recursive search would make
    # the command's behaviour depend on tree shape, which is exactly
    # the kind of surprise a validation tool must not introduce.
    manifest = path / "blueprint.yaml"
    return manifest if manifest.is_file() else None


# -----------------------------------------------------------------------------
# Debug detection
# -----------------------------------------------------------------------------
def _debug_requested() -> bool:
    """Return ``True`` if ``--debug`` was passed on this invocation.

    The ``validate`` command does not receive the root callback's
    ``ctx.obj`` — Typer does not thread context through the callback
    when the callback lives on the root app. Reading ``sys.argv``
    directly is the same fallback ``main()`` uses in
    ``_resolve_debug_flag``; the two are consistent by construction
    because both scan the same argv.

    Kept as a separate function (rather than an inline ``in`` check)
    for two reasons:

    * **Testability.** The test suite can monkeypatch
      ``_debug_requested`` to simulate ``--debug`` without touching
      ``sys.argv``, which is a shared global.
    * **Readability.** The command body reads as a single
      ``if _debug_requested():`` branch, which names the intent,
      rather than as an inline string scan whose significance the
      reader has to infer.

    The check is intentionally a *literal* ``in`` scan, not a full
    argument parse: the root callback has already validated
    ``--debug`` by the time the command body runs, so the only job
    of this helper is to *recall* whether it was set. A full parse
    here would duplicate Typer's parsing and could disagree with it;
    a literal scan cannot disagree with itself.

    ``--json`` is **not** read here. It is a command-level option and
    Typer threads it through the callback signature as a boolean
    parameter, so no argv scan is needed (or wanted — a scan would
    duplicate Typer's parsing for a flag whose value we already
    have). Direct callers get the coercion from ``_coerce_bool``
    instead; that helper handles the ``OptionInfo`` sentinel that
    would otherwise leak through a direct call.

    See the module docstring's "Deferred improvements" section for
    the plan to replace this with a ``ctx.obj`` read once the
    registry threads context through to single-command callbacks.
    """
    return "--debug" in sys.argv


# -----------------------------------------------------------------------------
# Rendering — dispatch
# -----------------------------------------------------------------------------
def _render_dispatch(
    result: Ok[Blueprint] | Err[list[ValidationError]],
    *,
    source: str,
    as_json: bool,
) -> int:
    """Route to the correct renderer and return the process exit code.

    A single, obvious branch. The two renderers do not share state,
    do not call each other, and do not know about each other; the
    dispatch is the only place ``as_json`` is consulted. That keeps
    the flag from leaking into either renderer's signature (a
    renderer that knew about ``--json`` would be doing two jobs) and
    keeps the exit-code decision structurally identical between
    modes — both ``_render_human`` and ``_render_json`` return the
    same value for the same ``Result``.

    ``as_json`` is *always* a real ``bool`` here, not an
    ``OptionInfo``. The caller (``validate``) coerces the parameter
    before passing it in — see ``_coerce_bool``'s docstring. Trusting
    the caller here is correct because the trust is enforced at the
    single boundary where the untrusted value enters the module.

    Why a helper and not an inline ``if as_json: ... else: ...``
    in the command body:

    * The command body already reads as a three-step flow (resolve,
      load, render). Inlining the branch would add a fourth step
      whose only job is to choose between two calls, which obscures
      the flow rather than clarifying it.
    * Test suites that want to prove "the command renders through
      exactly one renderer" can inspect this function's source
      rather than trying to trace two call sites in the command.
    * Future modes (a ``--format=yaml`` added later, say) become
      new branches here and nowhere else.
    """
    if as_json:
        return _render_json(result, source=source)
    return _render_human(result, source=source)


# -----------------------------------------------------------------------------
# Rendering — human mode
# -----------------------------------------------------------------------------
def _render_human(
    result: Ok[Blueprint] | Err[list[ValidationError]],
    *,
    source: str,
) -> int:
    """Render a loader result as human-readable text.

    Kept as a separate function (rather than inlined) so the JSON
    renderer can sit beside it with an identical signature — that
    symmetry is what lets ``_render_dispatch`` be one line, and what
    lets a reader compare the two modes side by side without
    hunting through control flow.

    The parameter types are the concrete instantiations
    ``BlueprintLoader.load()`` returns — ``Ok[Blueprint]`` on success,
    ``Err[list[ValidationError]]`` on failure — not bare ``Ok`` /
    ``Err``. Under ``mypy --strict``, bare generics are rejected as
    ``type-arg`` errors because they silently become ``Ok[Any]`` /
    ``Err[Any]`` and erase the payload types a reader (and the type
    checker) needs. The ``isinstance(result, Ok)`` narrow below then
    gives ``result.value`` the type ``Blueprint`` and
    ``result.error`` the type ``list[ValidationError]``, so the
    attribute accesses inside are checked rather than inferred as
    ``Any``.

    Each ``ValidationError`` also carries a machine-readable ``code``
    (see ``forge.loading.errors``). Human output does not surface it —
    the ``code`` exists for the JSON renderer and for automation that
    consumes ``ValidationError`` directly. Do not "clean up" the
    unused field: it is populated and preserved by design.
    """
    if isinstance(result, Ok):
        bp = result.value
        ui.success(f"OK: {bp.id} v{bp.version}")
        return int(ExitCode.SUCCESS)  # 0

    errors = result.error
    # Summary line first, then detail. A user with a 40-error report
    # wants to know "how bad is it" before scrolling. The count is
    # authoritative (it is ``len(errors)``) rather than inferred from
    # the rendered lines, so it stays correct if a future formatter
    # collapses duplicates.
    ui.error(f"{len(errors)} validation error(s) in {source}:")
    for error in errors:
        # ``ValidationError.format()`` is the single, tested rendering
        # for a validation error; using it here means the ``validate``
        # command's output matches every other place errors are shown
        # (``create`` failures, future doctor output, JSON consumers
        # that read the same fields). The alternative — hand-formatting
        # here — would drift the first time someone fixed ``format()``
        # and forgot this call site.
        ui.info("  • " + error.format())
    return int(ExitCode.USER_ERROR)  # 1


# -----------------------------------------------------------------------------
# Rendering — JSON mode
# -----------------------------------------------------------------------------
def _render_json(
    result: Ok[Blueprint] | Err[list[ValidationError]],
    *,
    source: str,
) -> int:
    """Render a loader result as the versioned JSON envelope.

    The envelope is constructed as a ``ValidateOutput`` DTO *before*
    serialisation. Two reasons:

    * The DTO's field set, nullability, and ``schema_version``
      literal are enforced at construction. A change that would
      violate the contract (a new required field, a renumbered
      version, a missing hint) fails here, at the one place the
      shape is built — not at snapshot time, three layers away.
    * ``render_validate_json`` is a pure serialiser. It does not
      reshape, rename, or drop fields; it takes the DTO and produces
      the string. That separation is what makes the JSON contract
      reviewable in ``forge/cli/schemas/validate_output.py`` and
      testable in isolation.

    Stream discipline: JSON output goes to **stdout only**. Nothing
    here writes to stderr. The ADR-0012 contract says machine
    output is product (stdout), commentary is stderr; a validation
    report is product, so stderr stays empty. A future test that
    asserts ``captured.err == ""`` in JSON mode is proving this
    invariant; it is not just checking a convenience.

    Exit codes match the human renderer exactly (0 on success, 1 on
    validation failure). The envelope is a *presentation* of the
    result, not a replacement for its exit-code meaning — a consumer
    that branches on ``$?`` gets the same answer in both modes.
    """
    # Local imports keep the JSON path lazy: importing ``renderers``
    # at module level pulls in every renderer the CLI ships (JSON,
    # table, human, etc.), and a user who runs ``forge validate``
    # in human mode pays the cost of loading modules they never use.
    # Local imports are cheap here and only execute once per run.
    from forge.cli.renderers import render_validate_json
    from forge.cli.schemas.validate_output import ValidateError, ValidateOutput

    if isinstance(result, Ok):
        bp = result.value
        output = ValidateOutput(
            valid=True,
            source=source,
            blueprint_id=bp.id,
            version=bp.version,
        )
        ui.emit(render_validate_json(output))
        return int(ExitCode.SUCCESS)  # 0

    # Failure path: build one ``ValidateError`` per loader error.
    # Field-by-field (rather than ``ValidateError(**err.__dict__)``
    # or similar) so that a field added to the loader's
    # ``ValidationError`` (e.g. ``severity``) does not *silently*
    # leak into the JSON contract — it becomes a compile-time
    # concern in this one place, and the contract document
    # (``CLI_VALIDATE_OUTPUT.md``) gets updated in the same change
    # or not at all.
    output = ValidateOutput(
        valid=False,
        source=source,
        errors=[
            ValidateError(
                path=err.path,
                message=err.message,
                hint=err.hint,
                code=err.code,
                line=err.line,
                column=err.column,
            )
            for err in result.error
        ],
    )
    ui.emit(render_validate_json(output))
    return int(ExitCode.USER_ERROR)  # 1
