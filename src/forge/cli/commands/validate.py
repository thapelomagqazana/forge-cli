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
for ``typer.Argument``, but the command body no longer raises
``typer.Exit``. ``--debug`` behaviour is unchanged: on an escaping
exception, the command re-raises, and ``main()``'s ``handle_exception``
(with ``debug=True``) surfaces the traceback.

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
The command obeys the project's stable exit-code contract (see
``docs/reference/exit-codes.md``). Its exit code is **total** — every
documented path produces one of three values, never anything else:

* ``0`` — validation succeeded.
* ``1`` — the blueprint is invalid (schema violations, model parity
  errors, missing templates, unreadable files, malformed YAML), or
  the loader itself raised an unexpected exception while validating.
* ``2`` — usage error (missing path, path does not exist, unreadable
  path, or directory without a ``blueprint.yaml``) — emitted by
  Typer or by the command body before the loader runs.

**Exit code 5 is never returned by this command.** The loader is
designed to be total (every expected failure is an ``Err``), but the
loader runs inside a real process — a missing bundled resource, an
OS-level permission error, a bug in a phase — and any of those could
raise. From the *user's* perspective, "the loader crashed on my file"
and "my file is invalid" are the same problem (their file did not
validate), so the command converts an escaping exception into exit
code 1 with a rendered error, not into exit code 5 with an internal-
error message. Scripts and CI can branch on ``$?`` and be sure that
exit 5 (if it ever appears) means "the CLI itself has a bug that this
command could not contain" — never "your blueprint is invalid".

Under ``--debug``, the exception is re-raised rather than converted,
so a contributor diagnosing the underlying bug gets the full
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
Human mode (default): one error per line, using
``ValidationError.format()``, which the loader guarantees renders
``source:line:col: path: message — hint: fix``. Greppable, copy-
pasteable, and readable in a narrow terminal. A leading summary line
(``N validation error(s):``) orients the user before the list, and a
trailing success line (``OK: <id> v<version>``) tells them the
command did something when there are no errors.

The ``--json`` flag is *not* added here. It was scoped to a separate
task, and adding it now would couple this command to a serialization
module that does not yet exist. When the JSON output task lands, the
change is additive: a new flag, a new branch in ``_render``, and no
change to the pipeline. The command is structured so that change is a
small, obvious diff rather than a rewrite.

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
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from forge.cli import ui
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
        # This is a usage error: the caller pointed at something that
        # is not a blueprint. Return code 2 (Typer's own usage-error
        # code) so scripts cannot distinguish "forgot PATH" from
        # "PATH is not a blueprint folder" — both mean "you called me
        # wrong", not "your blueprint is invalid".
        ui.error(
            f"No blueprint.yaml found in directory: {path}\n"
            f"  hint: A blueprint directory must contain a "
            f"'blueprint.yaml' at its root."
        )
        return 1

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
    # could raise. From the *user's* perspective, "the loader crashed
    # on my file" and "my file is invalid" are the same problem: their
    # file did not validate. So we convert any escaping exception into
    # a single rendered error and return exit code 1 (validation
    # failed), not 5 (internal error). The command's exit-code
    # contract becomes total: 0, 1, or 2 — never 5.
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
        # Convert the exception into the same error surface a
        # validation failure produces, so scripts and CI see one
        # kind of failure (exit 1) for the same command — not an
        # internal-error exit 5 that would suggest the CLI itself
        # is broken.
        ui.error(f"Could not validate {path}: {exc}")
        ui.hint(
            "Run with --debug for a full traceback, or report this "
            "at https://github.com/thapelomagqazana/forge-cli/issues"
        )
        return 1

    # ------------------------------------------------------------------
    # 3. Render.
    #
    # A helper keeps the command body read as a three-step flow
    # (resolve, load, render) rather than interleaving formatting
    # logic with control flow. It also gives the future ``--json``
    # branch a natural home and a place to add its tests without
    # touching the command body.
    #
    # ``_render`` already returns 0 or 1 (the exit code), so the
    # command simply forwards it to ``main()``.
    # ------------------------------------------------------------------
    return _render(result, source=str(manifest))


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
    """
    return "--debug" in sys.argv


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------
def _render(
    result: Ok[Blueprint] | Err[list[ValidationError]],
    *,
    source: str,
) -> int:
    """Render a loader result and return the process exit code.

    Extracted from the command body so:

    * The future ``--json`` flag has an obvious insertion point — a
      sibling ``_render_json`` function and a branch in the command
      body, no restructuring of the pipeline.
    * The human-mode output can be unit-tested by calling ``_render``
      directly with a constructed ``Result``, without spinning up
      Typer's ``CliRunner`` for a formatting assertion. Typer tests
      are slower and noisier; reserving them for command-level tests
      keeps the suite fast.
    * The exit-code decision lives in exactly one place. Adding a new
      error category (e.g. a warning that does not change the exit
      code) means editing one function, not hunting for ``return 1``
      scattered through the command.

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
    """
    if isinstance(result, Ok):
        bp = result.value
        ui.success(f"OK: {bp.id} v{bp.version}")
        return 0

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
        # (``create`` failures, future doctor output, future --json
        # consumers that read the same fields). The alternative —
        # hand-formatting here — would drift the first time someone
        # fixed ``format()`` and forgot this call site.
        ui.info("  • " + error.format())
    return 1
