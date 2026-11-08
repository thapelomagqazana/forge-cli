"""The blueprint loader — the one pipeline everything else calls.

Why this module exists
----------------------
Loading a blueprint is deceptively complex: it involves safe YAML parsing,
JSON Schema validation, Pydantic model validation, and filesystem checks
for referenced template files. Every consumer (`forge validate`, discovery,
`forge create`, future importers) needs the *same* pipeline with the *same*
error shape. If each consumer assembled its own pipeline, rules would drift
within a sprint.

Design principles
-----------------
* **One shape, always.** `load()` returns
  `Result[Blueprint, list[ValidationError]]` — never a bare exception, never
  `None`, never a partial object. Callers cannot accidentally ignore failure
  because `Result` is not truthy and `.unwrap()` raises loudly.
* **Aggregate, don't short-circuit.** Except for YAML-parse failures (there
  is no document to check), every phase contributes what it can. A user
  fixes one file in one pass, not N passes.
* **Hints are mandatory.** Every `ValidationError` carries a `hint`.
  A missing hint is a bug, not a cosmetic issue — it means the user is
  being told *what* is wrong without being told *what to do*.
* **Phase order is intentional.** See the flow diagram below. The schema
  is authoritative (ADR-0007); the model is a typed projection (ADR-0008);
  filesystem checks run last because they depend on a structurally valid
  document (a partially-valid model would have ambiguous file references).

Phase flow
----------
    raw bytes ─► parse YAML ─► schema ─► model ─► fs checks ─► Ok(Blueprint)
                     │            │         │          │
                     │            │         │          └──► template errors
                     │            │         └─────────────► model errors
                     │            └───────────────────────► schema errors
                     └────────────────────────────────────► parse error
                                                                       │
                                                                       ▼
                                                    Err([ValidationError, ...])

Short-circuit rules (deliberate, and covered by tests):
    * YAML parse failure  → return immediately (no document to check).
    * Schema failure      → return immediately (schema is the referee;
                            model errors would echo the same problems with
                            less precise paths).
    * Model failure       → return immediately (filesystem refs on a
                            structurally invalid document are ambiguous).
    * Filesystem failure  → return immediately (nothing further to check;
                            template existence is the last phase).
    * `check_files=False` → skip the filesystem phase entirely (used by
                            discovery, which only needs metadata and cannot
                            resolve template paths for in-memory candidates).
"""

from __future__ import annotations

from pathlib import Path

from forge.loading.aggregator import from_phase
from forge.loading.errors import ValidationError
from forge.loading.fs_checks import check_templates
from forge.loading.result import Err, Ok, Result
from forge.loading.yaml_parser import ParsedDocument, parse_yaml
from forge.models import Blueprint
from forge.verification.handlers import model_errors, schema_errors


class BlueprintLoader:
    """Load, validate, and type a single blueprint from disk.

    Stateless and side-effect-free apart from reading the input file
    (and, when `check_files=True`, stat-ing referenced template files).
    Safe to reuse across calls and across threads.
    """

    def load(
        self,
        path: Path,
        *,
        check_files: bool = True,
    ) -> Result[Blueprint, list[ValidationError]]:
        """Load and validate the blueprint at `path`.

        Parameters
        ----------
        path
            Filesystem path to a blueprint YAML file. May be relative;
            resolved against the current working directory by the caller
            if absolute paths are needed for logging.
        check_files
            When True (default), verifies that every template file the
            blueprint references actually exists under the blueprint's
            directory. Set to False for metadata-only callers (e.g.
            discovery of in-memory candidates) that cannot resolve
            template paths meaningfully.

        Returns
        -------
        Result[Blueprint, list[ValidationError]]
            `Ok(blueprint)` on success. `Err(errors)` otherwise, with
            at least one entry. Every error carries `path`, `message`,
            `hint`, and `source`.

        Raises
        ------
        TypeError
            If a future refactor changes ``parse_yaml``'s declared
            return type in a way this method does not anticipate. The
            raise is a tripwire for a programming error, not a user
            error; it is a plain ``TypeError`` (not an ``AssertionError``)
            so it survives ``python -O``.
        """
        source = str(path)

        # ------------------------------------------------------------------
        # Phase 0 — read the file.
        #
        # We read *bytes*, not text, so that encoding problems surface as
        # a located error from the YAML parser rather than as a Unicode
        # traceback. The loader owns the I/O boundary; downstream phases
        # receive `bytes` and never touch disk for the document itself.
        # ------------------------------------------------------------------
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return Err(
                [
                    ValidationError(
                        path="",
                        message=f"cannot read file: {exc.strerror or exc}",
                        hint=(
                            "Check the path and permissions; the CLI must be "
                            "able to read the file."
                        ),
                        source=source,
                    )
                ]
            )

        # ------------------------------------------------------------------
        # Phase 1 — parse YAML.
        #
        # `parse_yaml` returns either a ParsedDocument or a ValidationError.
        # Using a return value (not an exception) keeps this method's
        # control flow flat: every phase follows the same "check, maybe
        # return, else continue" shape.
        # ------------------------------------------------------------------
        parsed = parse_yaml(raw, source=source)
        if isinstance(parsed, ValidationError):
            # YAML parse failure: no document to validate. Return the
            # located parse error (with line/column if available).
            return Err([parsed])

        if not isinstance(parsed, ParsedDocument):
            # ``parse_yaml`` is declared to return
            # ``ParsedDocument | ValidationError``. Reaching here means a
            # future refactor changed that contract without updating
            # this caller — a programming error, not a user error.
            #
            # ``raise TypeError`` (not ``assert``) because:
            #
            # 1. ``assert`` is stripped under ``python -O``. The
            #    invariant this check enforces must survive every
            #    execution mode: an incorrect return type reaching
            #    ``parsed.data`` below would produce a confusing
            #    ``AttributeError`` (or worse, silently succeed with
            #    the wrong data shape).
            # 2. ``bandit`` flags ``assert`` in production code
            #    (``S101``) for exactly the ``-O`` reason. ``raise``
            #    is the idiomatic replacement; it is *also* the fix
            #    that ``bandit`` recommends.
            # 3. ``mypy`` and ``pyright`` narrow ``parsed`` to
            #    ``ParsedDocument`` after this block, so the
            #    ``parsed.data`` reference below is typed correctly
            #    without a ``# type: ignore`` or ``cast``.
            #
            # The branch is unreachable by construction today. It
            # exists as a tripwire for future contract violations, so
            # a future contributor who changes ``parse_yaml``'s return
            # type sees an immediate, clear failure naming this file
            # and this contract — rather than discovering the problem
            # three phases downstream.
            raise TypeError(
                f"parse_yaml() returned unexpected type "
                f"{type(parsed).__name__!r}; expected ParsedDocument or "
                f"ValidationError. This is a Forge bug — see "
                f"forge/loading/yaml_parser.py."
            )

        # ``parsed`` is narrowed to ``ParsedDocument`` for both ``mypy``
        # (via the ``isinstance`` check above) and the runtime (via the
        # ``raise`` above). No ``assert`` needed.

        # ------------------------------------------------------------------
        # Phase 2 — JSON Schema validation.
        #
        # The schema is authoritative (ADR-0007). `schema_errors` returns
        # *all* violations, not just the first. We prefix each error's
        # dotted path with the phase name so a caller can distinguish
        # schema-layer problems from model-layer ones in aggregate output
        # — useful for debugging parity bugs.
        # ------------------------------------------------------------------
        schema_errs = schema_errors(parsed.data)
        if schema_errs:
            return Err(from_phase("schema", source, schema_errs))

        # ------------------------------------------------------------------
        # Phase 3 — Pydantic model validation.
        #
        # Reached only if the schema accepted the document. If the model
        # rejects it now, that is a parity bug between schema and model
        # (ADR-0008) — we still surface it as a normal validation error,
        # but the phase label ("model") makes it obvious in logs and
        # tests that this should never happen in practice. The parity
        # test suite is the primary guard; this is the production net.
        # ------------------------------------------------------------------
        model_errs = model_errors(parsed.data)
        if model_errs:
            return Err(from_phase("model", source, model_errs))

        # Both validators agree: parse into the typed model. This is the
        # *only* place a Blueprint is constructed during loading, so every
        # consumer downstream is guaranteed to hold a fully validated one.
        blueprint = Blueprint.model_validate(parsed.data)

        # ------------------------------------------------------------------
        # Phase 4 — filesystem checks (optional).
        #
        # A blueprint that references a template file which does not exist
        # is not *usable*, even though it is structurally valid. Treating
        # missing templates as load errors (rather than crashes during
        # `forge create`) means users get the same batched, hinted
        # reporting they get for schema violations.
        #
        # Discovery passes `check_files=False` because it stages candidates
        # in a temp directory whose templates cannot exist by construction;
        # failing there would produce false positives.
        # ------------------------------------------------------------------
        if check_files:
            fs_errs = check_templates(
                blueprint,
                blueprint_root=path.parent,
                source=source,
            )
            if fs_errs:
                return Err(fs_errs)

        # ------------------------------------------------------------------
        # Success.
        #
        # Returning the model (not a raw dict, not None) means callers
        # never re-parse after validating — one of the classic "validated
        # the wrong dict" bug sources is removed structurally.
        # ------------------------------------------------------------------
        return Ok(blueprint)
