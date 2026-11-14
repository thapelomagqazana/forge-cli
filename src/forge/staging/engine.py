"""StagingEngine — render the plan into a hidden tree, atomically.

Purpose
-------
Turn a `PlannedFile[]` (plus a variables mapping and a blueprint root)
into a fully-rendered staging directory that the next phase (verify,
then commit) can consume — without ever touching the destination root.

Contract
--------
    StagingEngine().stage(
        plan=[PlannedFile],
        variables=Mapping[str, object],
        blueprint_root=Path,
        destination_root=Path,
        progress: ProgressSink = null_sink,
        renderer: TemplateRenderer | None = None,
    ) -> Result[StagingResult, RenderingError]

Guarantees
----------
* **No destination writes.** The engine holds the destination root only
  to construct the staging area; it never opens a file there. The
  staging area is the sole writer, and its typed API cannot address a
  path outside itself.
* **All-or-nothing.** Any per-file failure → every finding is
  aggregated into one `RenderingError`, the staging tree is destroyed,
  and the destination is left byte-identical (asserted by the test
  suite via a filesystem fingerprint).
* **Deterministic order.** Files are processed in plan order; the
  emitted manifest is sorted so downstream diffing is stable.
* **Progress is data.** Every file emits exactly one `ProgressEvent`
  (`render` or `copy`); a `start` event precedes and a `finish` event
  follows. The engine never prints — the CLI renders progress, tests
  capture into a list.
* **Ownership is explicit.** On success the staging tree is renamed to
  a stable `*-ready` sibling and returned as a `StagingResult`. The
  caller owns it from that point and must call `destroy()` on any
  unrecoverable downstream error.

Why this shape (and not "just render into the destination")?
-------------------------------------------------------------
The destination is the user's project. A partial render there is a
state we cannot recover from: half a pyproject.toml, missing imports,
nothing that `forge` can recognise as its own. Staging is the only way
to make "either the whole project exists or none of it does" a property
of the code, rather than a promise in a docstring.

Why ``stage()`` accepts an injected ``renderer``
------------------------------------------------
``render_or_copy`` — the per-file transition function this engine
calls — was designed with an optional ``renderer`` parameter
specifically so callers could substitute one: tests need to *observe*
rendering (to assert that copy-kind files are never passed through it),
and future features (a dry-run preview, a rendering metrics tool, a
``forge render`` command that renders without staging) will need to
*control* it.

An earlier version of this engine hardcoded ``renderer=TemplateRenderer()``
in its call to ``render_or_copy``, silently defeating that seam. The
visible consequence was a test — ``test_copy_kind_is_not_passed_through_renderer``
in ``tests/unit/staging/test_copy_kind.py`` — that tried to spy on the
renderer by patching its class method, and failed because the spy and
the engine were consulting different class objects (a facade alias vs.
the concrete module, which happened to diverge in the current import
graph). The fix is not "make the spy more careful": the fix is to
honour the injection seam the per-file loop already advertises.

Threading the parameter through keeps the default behaviour identical
(a caller that does not pass a renderer gets the same lazy
construction ``render_or_copy`` already performs — copy-kind files
never construct one, render-kind files do) while making the seam
usable for the callers it was designed for.

Why ``stage()`` has a ``# type: ignore[no-any-unimported]``
-----------------------------------------------------------
``TemplateRenderer`` is imported from ``forge.rendering``, whose
façade lazy-loads the concrete class from ``forge.rendering.renderer``,
which in turn imports ``jinja2`` — a package gated behind
``ignore_missing_imports`` in ``pyproject.toml``. Mypy can therefore
see the *name* ``TemplateRenderer`` but cannot resolve its shape, and
treats any parameter annotated ``TemplateRenderer | None`` as
``Any | None``. Strict mode's ``[no-any-unimported]`` check flags
that at the ``def`` line.

The suppression is narrowly scoped: it covers only
``[no-any-unimported]``, only on this ``def`` line. The parameter's
*runtime* contract is unchanged — the caller passes either a
``TemplateRenderer`` instance or ``None``, and passing anything else
fails at ``.render(...)`` with a clear ``AttributeError``. If
``jinja2`` ever gains type stubs (or the project drops
``ignore_missing_imports`` for it), the ignore becomes unused and
mypy's ``unused-ignore`` check — on under strict mode — will force
its removal in the same PR that lands the stubs. That is the desired
signal: the ignore is a *placeholder for an external dependency's
gap*, not a permanent part of this module's contract.

Why ``progress: ProgressSink = null_sink`` needs a ``cast``
-----------------------------------------------------------
``ProgressSink`` is a ``Protocol`` with ``__call__(self, event:
ProgressEvent) -> None``. ``null_sink`` is a plain function with the
same signature: ``def null_sink(_event: ProgressEvent) -> None``.

Under normal structural typing, a plain function satisfies a
callable-shape Protocol, so mypy should accept ``null_sink`` as a
default value for a ``ProgressSink`` parameter. But it does not, for a
subtle reason: **Protocols with a single ``__call__`` are treated as
"callable protocols," and mypy's check for "is this function a valid
``ProgressSink``?" compares the parameter *names*.**

``null_sink`` names its parameter ``_event`` (a deliberate choice — the
underscore signals "unused"). The protocol names it ``event``. Under
``[assignment]``, mypy flags the default because ``_event != event``.

The fix is a one-line ``cast`` that says "yes, this function satisfies
the protocol; trust me." The alternative — renaming ``_event`` to
``event`` in ``null_sink`` — would silence the diagnostic but defeat
the convention that unused parameters are underscore-prefixed, and
would make ``null_sink``'s signature look like it *uses* the event
when it does not. A future reader of ``null_sink`` would wonder why
the parameter is named as if it mattered.

The ``cast`` documents the intentional gap between "protocol requires
an ``event`` parameter" and "this sink ignores the event." It is
narrow (one call site), honest (the runtime contract really is
satisfied — ``null_sink`` accepts any ``ProgressEvent`` and returns
``None``), and it keeps ``null_sink``'s signature truthful.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from forge.loading.result import Err, Ok, Result
from forge.planning import PlannedFile, PlannedFileKind
from forge.rendering import TemplateRenderer
from forge.safety import StagingArea
from forge.staging._renderer_loop import render_or_copy
from forge.staging._validate import validate_staged_tree
from forge.staging.errors import Finding, RenderingError
from forge.staging.progress import ProgressEvent, ProgressSink, null_sink
from forge.staging.result import StagingResult

# Suffix appended to the staging directory's name when ownership is
# transferred to the caller. Kept as a module constant so tests can
# assert on it without hard-coding a string in multiple places.
_READY_SUFFIX = "-ready"


class StagingEngine:
    """Render every planned file into a hidden staging tree.

    Stateless and reusable. Safe to call concurrently for *different*
    destination roots; each call owns its own staging directory, and
    `StagingArea.for_root` uses `tempfile.mkdtemp` so collisions between
    concurrent calls are impossible by construction.
    """

    # ``[no-any-unimported]`` is suppressed on the ``def`` line only.
    # See the module docstring's "Why ``stage()`` has a
    # ``# type: ignore[no-any-unimported]``" section for the reasoning.
    # The suppression is load-bearing for the *type checker* (which
    # cannot follow ``TemplateRenderer`` through the ``forge.rendering``
    # façade into the ``jinja2``-gated module) but does not affect
    # runtime behaviour: the parameter is a ``TemplateRenderer`` (or
    # ``None``) at call time, and passing anything else fails at
    # ``.render(...)`` with a clear ``AttributeError``.
    def stage(  # type: ignore[no-any-unimported]
        self,
        plan: list[PlannedFile],
        *,
        variables: Mapping[str, object],
        blueprint_root: Path,
        destination_root: Path,
        # ``cast`` is load-bearing under mypy strict: ``null_sink``'s
        # parameter is named ``_event`` (underscore-prefixed for
        # "unused"), while ``ProgressSink.__call__`` names its
        # parameter ``event``. Mypy treats single-``__call__`` Protocols
        # as callable protocols and compares parameter *names* under
        # ``[assignment]``, so the default fails even though the
        # function's runtime contract (accept any ``ProgressEvent``,
        # return ``None``) is satisfied. The cast says "this function
        # really does implement the protocol, trust me" without lying
        # about either side: ``ProgressSink``'s contract is preserved,
        # and ``null_sink``'s ``_event`` underscore convention is
        # preserved. See the module docstring's "Why ``progress:
        # ProgressSink = null_sink`` needs a ``cast``" section.
        progress: ProgressSink = cast(ProgressSink, null_sink),
        renderer: TemplateRenderer | None = None,
    ) -> Result[StagingResult, RenderingError]:
        """Render every planned file into a hidden staging tree.

        Parameters
        ----------
        plan
            The files to produce, in processing order. Order is
            preserved by the per-file loop (and the emitted manifest
            is *sorted* for downstream diffing; processing order and
            reporting order are deliberately different).
        variables
            Resolved variable values. Snapshotted into a plain ``dict``
            at entry so a template that mutates its context cannot
            affect a sibling's render.
        blueprint_root
            Directory the blueprint's ``source:`` paths are resolved
            against. Never the process CWD — see
            ``_renderer_loop.render_or_copy``'s docstring for the
            determinism argument.
        destination_root
            Directory the committed files will end up in. The engine
            uses it only to construct the staging area; it never
            writes to ``destination_root`` itself, and it never creates
            it (the commit phase does, via the first ``os.replace``'s
            ``mkdir -p`` on the target's parent). See ADR-0019 and
            ``forge.safety.staging``'s "Where the staging directory
            lives" section.
        progress
            Advisory sink for progress events. The engine emits exactly
            one ``render`` or ``copy`` event per planned file, plus one
            ``start`` before the loop and one ``finish`` after; the
            sink is responsible for presenting them. A sink that
            raises will abort the run — that is the correct behaviour
            for a caller-supplied callback, which is untrusted code
            from the engine's perspective.
        renderer
            Optional injected ``TemplateRenderer``. Defaulted to
            ``None`` so a caller that does not care about rendering
            gets the same behaviour as before: ``render_or_copy``
            constructs one lazily, and only for render-kind files
            (copy-kind files never construct a renderer, since they
            do not call its methods). A caller that *does* care —
            tests that want to observe rendering, future features
            that want to substitute a preview renderer — passes its
            own. See the module docstring's "Why ``stage()`` accepts
            an injected ``renderer``" section for the reasoning.

        Returns
        -------
        ``Ok(StagingResult)`` on success, with the staging tree
        renamed to a stable ``*-ready`` sibling and its manifest
        sorted for downstream stability. ``Err(RenderingError)`` if any
        file failed — the staging tree has been destroyed, the
        destination is byte-identical to its pre-run state, and the
        error carries every finding collected during the run.
        """
        # A single monotonic clock reading for the start event; per-file
        # timings are computed independently so one slow file does not
        # skew the others.
        started = time.monotonic()
        total = len(plan)

        # Materialise the variables once: templates that mutate their
        # context are a bug, and we do not want to hand each render a
        # different view if some future caller passes a lazy mapping.
        frozen_variables = dict(variables)

        findings: list[Finding] = []
        manifest: list[tuple[str, str]] = []

        # Announce the beginning of the phase before any work is done, so
        # a UI can render a spinner/progress bar from the first tick.
        progress(ProgressEvent(stage="start", index=0, total=total))

        # `read_only=False`: this engine is the authorised writer to a
        # staging tree. Read-only mode is reserved for dry-run paths that
        # must *never* write — see `forge.dryrun`.
        with StagingArea.for_root(destination_root, read_only=False) as stage:
            # Per-file render/copy loop. Failures are collected, not
            # raised, so the user sees every bad template in one report.
            for index, planned in enumerate(plan, start=1):
                file_started = time.monotonic()
                outcome = render_or_copy(
                    planned,
                    blueprint_root=blueprint_root,
                    variables=frozen_variables,
                    write_bytes=stage.write_bytes,
                    # Forward the injected renderer (default: ``None``).
                    # ``render_or_copy`` lazily constructs a
                    # ``TemplateRenderer`` only for render-kind files,
                    # so the common case is unchanged: copy-kind files
                    # never construct a renderer, and render-kind files
                    # get one instance per ``stage()`` call (not one
                    # per file, which the previous hardcoding
                    # accidentally achieved — the injection gives the
                    # caller control over construction *and* lifetime).
                    #
                    # Why forwarding (not "if renderer is None:
                    # renderer = TemplateRenderer()" in the engine):
                    # the whole point of the injection is that the
                    # *caller* controls the renderer's construction.
                    # Constructing a fresh one here — even when the
                    # caller passed ``None`` — would change the
                    # behaviour of a future caller that passed a
                    # renderer *and* a fresh-slate in the same call,
                    # which is a legitimate use case (a dry-run
                    # preview that uses a stub renderer, a metrics
                    # tool that wants to count constructor calls).
                    # Forwarding preserves the caller's choice;
                    # `render_or_copy`'s own laziness preserves the
                    # default behaviour.
                    renderer=renderer,
                )
                elapsed_ms = (time.monotonic() - file_started) * 1000.0

                progress(
                    ProgressEvent(
                        stage=("copy" if planned.kind is PlannedFileKind.COPY else "render"),
                        index=index,
                        total=total,
                        path=planned.destination,
                        elapsed_ms=elapsed_ms,
                    )
                )

                if not outcome.ok:
                    # ``render_or_copy``'s contract is that
                    # ``ok=False`` implies ``finding is not None``: the
                    # two fields are written together, in the same
                    # branch, for every non-success outcome. mypy cannot
                    # infer that from the ``Outcome`` dataclass's
                    # (``ok: bool; finding: Finding | None``) shape — it
                    # is not a discriminated union — so the explicit
                    # check below is *both* the type narrowing for the
                    # ``append`` and a runtime contract check.
                    #
                    # A real ``RuntimeError`` — not an ``assert`` —
                    # because the check must survive ``python -O``
                    # (which strips asserts). Without it, a future bug
                    # in ``render_or_copy`` that returned
                    # ``ok=False, finding=None`` would silently append
                    # ``None`` to ``findings``, and the failure would
                    # surface much later (when ``RenderingError``
                    # iterates the findings) as a confusing
                    # ``AttributeError`` on ``None``. The located
                    # ``RuntimeError`` below names the offending
                    # ``planned`` file and calls the contract violation
                    # what it is — a bug in Forge, not in the user's
                    # blueprint.
                    if outcome.finding is None:
                        raise RuntimeError(
                            "render_or_copy returned ok=False with no finding "
                            f"for destination {planned.destination!r}. "
                            "This is a contract violation in "
                            "forge.staging; please report it."
                        )
                    findings.append(outcome.finding)
                    continue

                manifest.append((planned.destination, planned.kind.value))

            # Abort path: if any file failed, do not validate, do not
            # hand back a staging tree. The context manager will remove
            # the staging directory on exit — that is our cleanup.
            if findings:
                return Err(
                    RenderingError(
                        findings=tuple(findings),
                        operation="staging",
                    )
                )

            # Success so far: sanity-check that the tree matches the plan
            # before we hand it to the caller. This catches staging-engine
            # bugs (missing write, unexpected extra write), not template
            # bugs — those already surfaced above.
            structural = validate_staged_tree(stage.path, tuple(manifest))
            if structural:
                return Err(
                    RenderingError(
                        findings=tuple(structural),
                        operation="staging",
                    )
                )

            # Ownership transfer: rename the staging directory to a
            # stable "ready" name *before* the context manager exits, so
            # the context manager's cleanup finds nothing to remove. The
            # caller's `StagingResult` then owns the tree and is
            # responsible for destroying it (on commit success or on any
            # unrecoverable error).
            ready_path = stage.path.with_name(stage.path.name + _READY_SUFFIX)
            stage.path.rename(ready_path)

            progress(
                ProgressEvent(
                    stage="finish",
                    index=total,
                    total=total,
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                )
            )

            # Manifest is sorted for stable downstream diffing. Order of
            # processing is preserved by `plan`; order of *reporting* is
            # deterministic.
            return Ok(
                StagingResult(
                    root=ready_path,
                    destination_root=destination_root,
                    manifest=tuple(sorted(manifest)),
                )
            )
