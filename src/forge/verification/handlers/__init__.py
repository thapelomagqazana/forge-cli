"""Verification handlers — the single validation funnel for the app.

This module owns *every* rule that decides whether a Forge input is
valid, whether that input is a **user answer** (flags or wizard
responses) or a **blueprint document** (parsed YAML/JSON). Two
consumers, one rulebook, one failure type per concern:

* ``validate_blueprint_inputs`` — user-supplied values, mode-agnostic.
  Called by both the flag-driven and interactive paths of
  ``forge create`` (ADR-0006). Adding an input rule means editing one
  function, not two call sites.

* ``validate_blueprint_document`` — a parsed blueprint document
  checked against the bundled JSON Schema (ADR-0007). Called by the
  blueprint loader, tests, and any future importer. Shape lives in
  the schema; this function only applies it.

Why both live in one module
---------------------------
They share three things that *must* stay aligned: the source of truth
for rules (the schema and this function respectively), the error
hierarchy they raise from (``ForgeError`` subclasses), and the set of
callers who need to trust that "verification" is a single boundary.
Splitting them across files would make the boundary a convention
rather than a fact, and conventions erode under deadline pressure.

What this module is *not* responsible for
-----------------------------------------
* **Filesystem state.** Whether a destination already exists, is
  writable, or escapes the permitted root is a filesystem concern.
  ``create.py::_generate`` owns it, because only that layer knows
  *what* would be written and whether the user asked to overwrite.
  Input failures are exit 1 (``UserInputError``); filesystem conflicts
  are exit 3 (``FilesystemConflictError``). Two classes, two codes,
  no ambiguity for scripts that branch on ``$?``.

* **Blueprint registry lookups.** ``validate_blueprint_inputs``
  receives ``blueprint_id`` already resolved by the caller. A second
  lookup here would create two sources of truth; trust the caller.

* **Per-variable rules.** The blueprint declares which variables
  exist, their types, defaults, and validators. This module accepts
  ``variables`` as-is and returns them unchanged. When the variable
  resolver lands, it will feed validated variables in, and this
  function will continue to normalize shape — not reinterpret
  contents.

Design invariants
-----------------
* **Pure functions.** No I/O, no prompting, no rendering. That makes
  them testable without ``tmp_path``, usable from a future SDK, and
  unambiguous about what they do.

* **Typed failures only.** Every rejection raises a ``ForgeError``
  subclass (``UserInputError`` or ``SchemaValidationError``).
  Callers never see bare ``ValueError`` / ``KeyError``.

* **Aggregated errors where the caller can act on them.** Schema
  validation collects *every* violation in one pass; missing-flag
  reporting collects *all* missing flags at once. Fixing an input
  should be one edit, not a ping-pong loop.

* **Precise pointers.** Every ``(json_pointer, message)`` pair in a
  ``SchemaValidationError`` locates the *specific* offending value,
  not its parent. ``jsonschema``'s default convention reports the
  parent for two keyword families — ``required`` and
  ``additionalProperties`` — and produces phantom ``if``-branch
  artefacts when an ``allOf``/``if-then`` fails. The helpers below
  translate the first into precise pointers and drop the second
  entirely, so the caller never sees a pointer that does not
  correspond to a real instance path.

* **Schema is read once.** ``_build_validator`` caches the parsed
  schema via ``forge.schemas.load_schema`` (``lru_cache``), so
  validation is a dict lookup plus traversal — no per-call file read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from forge.core.exceptions import SchemaValidationError, UserInputError
from forge.schemas import load_schema


# =============================================================================
# Result types
# =============================================================================
@dataclass(frozen=True, slots=True)
class ValidatedInputs:
    """The validated, mode-agnostic result of ``validate_blueprint_inputs``.

    Frozen and slotted so a caught result cannot be mutated by a
    downstream handler — the wizard's answers are a snapshot, not a
    scratchpad. Slots also shrink the per-instance cost, which matters
    for a type created once per command and passed to a generation
    pipeline that may spawn workers.

    Attributes:
        blueprint_id: The blueprint's canonical ID, exactly as passed
            by the caller. Not normalized (case, whitespace) — IDs are
            opaque identifiers and the registry owns their format.
        name: The project name, exactly as passed. Whitespace
            normalization is the caller's responsibility (``create.py``
            strips the wizard's answer); doing it here would silently
            change what the user typed, which is a surprise, not a fix.
        destination: The destination directory, normalized to an
            absolute ``str``. ``str`` rather than ``Path`` so the result
            is JSON-serializable without an adapter — a future ``--json``
            output can embed this field verbatim.
        variables: The blueprint-specific variables, passed through
            unchanged. ``dict[str, Any]`` because the eventual variable
            resolver may produce ints, bools, or lists; the funnel does
            not inspect them.
    """

    blueprint_id: str
    name: str
    destination: str
    variables: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Input validation (user-supplied values, mode-agnostic)
# =============================================================================
def validate_blueprint_inputs(
    *,
    blueprint_id: str,
    name: str,
    destination: str | Path | None,
    variables: dict[str, Any] | None = None,
) -> ValidatedInputs:
    """Apply input rules to collected values, mode-agnostically.

    Called by both the flag-driven and interactive paths of
    ``forge create``. This is the single funnel: adding a rule means
    editing this function, not two call sites.

    The function is **pure** — it does not read the filesystem, does
    not consult the blueprint registry, and does not render output.
    That is deliberate: purity is what lets it be tested without a
    ``tmp_path``, used from a future SDK, and reasoned about in
    isolation from the CLI.

    Args:
        blueprint_id: The blueprint's canonical ID. Already resolved by
            the caller (``create.py::_resolve_inputs``), so this
            function only guards against direct-caller mistakes.
        name: The project name. Must be non-empty and must not contain
            characters that cannot appear in a Python module name or a
            directory name (letters, digits, hyphens, underscores).
        destination: The resolved destination directory. ``None`` means
            "not yet chosen" and falls back to ``cwd / name``. An
            absolute path stays absolute; a relative path is resolved
            against the current working directory. The result is
            always an absolute ``str``.
        variables: Blueprint-specific variables. Passed through
            unchanged; per-variable rules are the blueprint's concern.

    Returns:
        A ``ValidatedInputs`` with all fields normalized.

    Raises:
        UserInputError: If ``blueprint_id`` or ``name`` is empty, or if
            ``name`` contains invalid characters. (Exit code 1.)
    """
    if not blueprint_id:
        raise UserInputError(
            "Blueprint ID is required.",
            hint="Pass a blueprint ID (e.g., python.fastapi.api).",
        )

    if not name:
        raise UserInputError(
            "Project name is required.",
            hint="Pass --name <project-name>.",
        )

    # Same rule the interactive prompt's inline validator applies, so
    # the two cannot disagree about what a legal name is. If this
    # check changes, ``create.py::_project_name_validator`` must change
    # in lockstep — the test suite asserts the subset relationship, but
    # expanding here without expanding there would create a wizard that
    # accepts what the funnel rejects.
    if not name.replace("-", "").replace("_", "").isalnum():
        raise UserInputError(
            f"Project name {name!r} contains invalid characters.",
            hint="Use only letters, digits, hyphens, and underscores.",
        )

    # Resolve the destination to an absolute path. ``expanduser``
    # handles ``~``; ``resolve`` normalizes ``..`` and symlinks. Both
    # are effectively no-ops for an already-absolute, already-clean
    # path, so the cost on the common case is one stat call — cheap,
    # and worth the guarantee that the returned path is canonical.
    resolved_destination = (
        Path(destination).expanduser().resolve()
        if destination is not None
        else (Path.cwd() / name).expanduser().resolve()
    )

    return ValidatedInputs(
        blueprint_id=blueprint_id,
        name=name,
        destination=str(resolved_destination),
        variables=dict(variables or {}),
    )


# =============================================================================
# Document validation (parsed blueprint vs. bundled JSON Schema)
# =============================================================================
@lru_cache(maxsize=1)
def _build_validator() -> Draft202012Validator:
    """Build (and cache) the Draft 2020-12 validator for the bundled schema.

    The schema's ``$id`` is stable, and ``Draft202012Validator`` caches
    its internal resolver, so a single instance can be reused across
    every validation call without re-resolving ``$ref`` targets. The
    schema document itself is loaded from ``forge.schemas.load_schema``,
    which is itself ``lru_cache``-d. Net effect: the schema is read and
    parsed exactly once per process.

    Constructing the validator *validates the schema* against the
    Draft 2020-12 meta-schema. A schema bug therefore fails the first
    call loudly, not the thousandth call subtly.
    """
    return Draft202012Validator(load_schema())


# The two ``jsonschema`` keywords whose ``absolute_path`` is the
# *parent* of the offending value rather than the value itself, with
# the field name visible only in the message. Every other keyword
# reports a precise path and is passed through unchanged.
_PARENT_PATH_KEYWORDS: frozenset[str] = frozenset({"required", "additionalProperties"})

# The three combinators whose parent error is a *rule* describing the
# instance, not a *violation* located at a specific value. Their
# actionable content lives in ``error.context``, subject to the
# phantom filter in ``_is_phantom``.
_COMBINATOR_KEYWORDS: frozenset[str] = frozenset({"allOf", "anyOf", "oneOf"})

# Placeholder used in the dedup key when ``error.validator`` is the
# ``Unset`` sentinel. Real ``jsonschema`` keywords are lowercase
# identifiers (``"required"``, ``"pattern"``, ``"allOf"``); a value
# wrapped in angle brackets cannot collide with any of them.
_NO_VALIDATOR_SENTINEL: str = "<no-validator>"


def _is_phantom(error: jsonschema.ValidationError) -> bool:
    """Return True if ``error`` is a phantom from a combinator's ``if`` branch.

    ``jsonschema``'s ``ValidationError.schema_path`` locates the
    keyword in the *schema* that produced the error. A genuine
    top-level violation has a ``schema_path`` like ``['required']``;
    a violation produced inside a combinator's ``if`` branch has a
    ``schema_path`` that includes ``'allOf'`` / ``'anyOf'`` /
    ``'oneOf'`` at a non-zero position, e.g.:

        ['properties', 'variables', 'items', '$ref',
         'allOf', 0, 'if', 'required']

    The attribute pair that identifies a phantom uniquely is:

    * **``schema_path`` contains a combinator keyword.** This is what
      distinguishes phantom ``if``-branch errors from genuine
      top-level violations (whose ``schema_path`` has no combinator).
    * **``absolute_path`` is empty.** This is what distinguishes
      phantom ``if``-branch errors from *real* violations inside a
      combinator's ``then`` branch. A real violation always points
      at a specific instance path (``['variables', 0]``, etc.); a
      phantom does not point anywhere, because it describes an
      ``if`` branch that was not supposed to apply.

    The two facts are *both* required:

    * Using only ``schema_path`` would drop real violations inside
      ``then`` — the very errors the caller most needs to see.
    * Using only ``absolute_path == []`` would drop genuine
      top-level ``required`` violations (e.g. a document missing
      ``id`` at the top level), which are the *expected* outcome for
      a schema-conformance check.

    Combining them is the only filter that keeps both the real
    errors and the phantoms' absence.

    A note on why the previous version of this filter failed: it
    used the *caller's* recursion state (``parent_has_path``) to
    decide whether an empty-path leaf was a phantom. That works when
    the parent combinator has a non-empty path, but not when the
    parent is the *document root* — a case where ``parent_has_path``
    is ``False`` for a phantom whose ``absolute_path`` is also empty.
    ``schema_path`` sidesteps the caller entirely, reading the fact
    from the error itself.
    """
    schema_path = list(error.schema_path)
    inside_combinator = any(segment in _COMBINATOR_KEYWORDS for segment in schema_path)
    return inside_combinator and not error.absolute_path


def _flatten_errors(
    errors: Iterable[jsonschema.ValidationError],
) -> list[jsonschema.ValidationError]:
    """Replace combinator wrapper errors with their actionable sub-errors.

    ``jsonschema`` reports an ``allOf`` failure as a single error at
    the *instance's* path, with the actionable violations in
    ``error.context``. Naively promoting every context child to a
    top-level error is wrong for two reasons:

    * **Phantom ``if``-branch errors.** When a combinator uses
      ``if/then``, ``jsonschema`` evaluates *both* the ``if`` and
      the ``then`` internally. Failures of the ``if`` branch (which
      are expected and are what makes ``if/then`` work) can surface
      in ``error.context`` as ``required`` errors with empty
      ``absolute_path``. ``_is_phantom`` identifies and drops them;
      without the filter, a document that clearly has an ``id``
      would report a phantom ``/id`` violation, because the
      phantom's message — ``"'id' is a required property"`` — comes
      from ``jsonschema``'s internal ``if`` evaluation and not from
      any real document state.

    * **Duplicate violations.** A combinator with multiple matching
      ``if`` branches can produce the same required-field error
      more than once. Callers expect each distinct violation to
      appear once — the near-copy test in ``test_schema_rejects``
      depends on this, and so does any human reading the error
      message.

    Only ``allOf``/``anyOf``/``oneOf`` are flattened. Every other
    keyword produces a leaf error whose pointer and message are
    already correct, so flattening them would lose information.

    A note on the dedup key's ``validator`` component:
    ``jsonschema`` types ``ValidationError.validator`` as
    ``str | Unset``. The ``Unset`` sentinel is only produced by
    manually-constructed errors; the validation pipeline itself
    always sets a concrete keyword. We narrow to ``str`` with an
    ``isinstance`` check, not with ``or``, because mypy cannot
    narrow on truthiness for a type whose stubs do not declare
    ``__bool__`` — the ``or`` form leaves the type as
    ``str | Unset`` and fails the ``arg-type`` check on
    ``seen.add``. ``isinstance`` narrows reliably (mypy understands
    it natively), and the fallback ``_NO_VALIDATOR_SENTINEL`` is
    chosen to be a value no real keyword can produce.
    """
    seen: set[tuple[tuple[str | int, ...], str, str]] = set()
    flat: list[jsonschema.ValidationError] = []

    def _visit(error: jsonschema.ValidationError) -> None:
        if _is_phantom(error):
            return

        if error.validator in _COMBINATOR_KEYWORDS and error.context:
            for sub in error.context:
                _visit(sub)
            return

        path = tuple(error.absolute_path)
        # ``error.validator`` is typed ``str | Unset``. ``isinstance``
        # narrows it to ``str`` in the true branch; the false branch
        # is the sentinel and becomes ``_NO_VALIDATOR_SENTINEL``.
        # The result is typed ``str`` end to end, so the dedup key
        # matches the set's declared type without a cast or a
        # type-ignore.
        validator_name = (
            error.validator if isinstance(error.validator, str) else _NO_VALIDATOR_SENTINEL
        )
        key = (path, validator_name, error.message)
        if key not in seen:
            seen.add(key)
            flat.append(error)

    for error in errors:
        _visit(error)

    return flat


def _quoted_field_from_message(message: str) -> str | None:
    """Return the field name referenced by a ``required``/``additionalProperties`` message.

    ``jsonschema`` formats the two relevant messages as:

    * ``"'<field>' is a required property"``
    * ``"Additional properties are not allowed ('<field>' was unexpected)"``

    Both wrap the field name in single quotes. We extract the first
    single-quoted token whose content looks like an identifier
    (``[A-Za-z_][A-Za-z0-9_]*``). That shape rule guards against a
    future ``jsonschema`` reword that puts prose in quotes — we
    would rather fail to disambiguate than append a nonsense path
    segment.

    Returns ``None`` when no plausible field name is present; the
    caller then falls back to the raw (parent) pointer, which is
    still better than a fabricated one.
    """
    for token in message.split("'"):
        token = token.strip()
        if token and token[0].isalpha() and all(c.isalnum() or c in {"_", "-"} for c in token):
            return token
    return None


def _escape_pointer_segment(segment: str | int) -> str:
    """Escape a single path segment per RFC 6901.

    ``~`` becomes ``~0`` and ``/`` becomes ``~1``. Array indices
    (``int``) are converted to their decimal string first — they
    cannot contain either character, so no escaping is needed for
    them, but the conversion keeps the return type uniform.
    """
    text = str(segment)
    return text.replace("~", "~0").replace("/", "~1")


def _format_pointer(error: jsonschema.ValidationError) -> str:
    """Render an error's location as a precise RFC 6901 JSON Pointer.

    ``jsonschema`` reports most violations at the offending *value's*
    path. Two keyword families are exceptions:

    * **``required`` and ``additionalProperties``** — the path is the
      *parent* object; the field name is only in the message. We
      append the field name so the pointer locates the actual
      problem (``/id`` rather than ``/``). Without this, a caller
      looking at the pointer would have to parse the prose to know
      *what* was missing — defeating the whole point of returning
      structured errors.

    * **``allOf`` / ``anyOf`` / ``oneOf``** — the error is a rule,
      not a violation. ``_flatten_errors`` has already replaced
      these with their children before this function runs, so this
      branch should not be reached; it is retained defensively so
      the formatter is safe to call on an un-flattened error.

    Array indices are rendered as decimal numbers (``/variables/0``),
    never as quoted strings — RFC 6901 requires that.

    Returns ``""`` (the RFC 6901 root pointer) when the error is at
    the document root *and* carries no field information — a rare
    case that the caller renders as ``/`` in human-facing output.
    """
    path: list[str | int] = list(error.absolute_path)

    if error.validator in _PARENT_PATH_KEYWORDS:
        field_name = _quoted_field_from_message(error.message)
        if field_name is not None:
            path.append(field_name)

    if not path:
        return ""
    return "/" + "/".join(_escape_pointer_segment(segment) for segment in path)


def validate_blueprint_document(
    document: Mapping[str, Any],
    *,
    source: str = "<memory>",
) -> None:
    """Validate a parsed blueprint document against the bundled schema.

    This is the *only* entry point by which a blueprint document may be
    checked for shape. Loaders, the CLI, tests, and future importers all
    call this function — rule changes happen in the schema, never here.

    The function **aggregates** every violation in one pass. Fixing an
    invalid blueprint should be one edit, not a ping-pong loop where
    each round reveals the next missing field.

    Args:
        document: A parsed blueprint (typically from YAML or JSON).
            Must be a mapping; any other type is rejected by the schema
            itself (``"type": "object"``), so we do not pre-check.
        source: A caller-supplied label used purely for error
            readability (usually a file path). Never inspected.

    Raises:
        SchemaValidationError: If the document does not validate. The
            exception carries ``(json_pointer, message)`` pairs sorted
            by path, so output is deterministic across runs.

    Notes:
        * We do not check ``schema_version`` separately. The bundled
          schema declares ``schema_version`` with ``"const": "1.0.0"``,
          so a document with a different version fails the standard
          validation pass and lands in the aggregated error list —
          no special-case branch, no second code path to test.
        * We do not catch ``jsonschema`` exceptions. A schema that
          cannot build a validator is a packaging bug, not a runtime
          condition; letting the exception propagate preserves the
          original traceback for the developer who must fix the wheel.
    """
    validator = _build_validator()

    # Flatten first, then format. A combinator error's ``absolute_path``
    # is the parent — formatting it before flattening would produce a
    # precise-looking but wrong pointer. Flattening also drops phantom
    # ``if``-branch artefacts (identified by ``_is_phantom``) and
    # deduplicates over-eager combinator evaluation, both of which
    # would otherwise reach the caller as spurious ``(pointer, message)``
    # pairs.
    raw_errors = _flatten_errors(validator.iter_errors(document))

    formatted = sorted(
        ((_format_pointer(e), e.message) for e in raw_errors),
        key=lambda pair: (pair[0], pair[1]),
    )

    if formatted:
        raise SchemaValidationError(source=source, errors=tuple(formatted))


# =============================================================================
# Public re-exports
# =============================================================================
# The public surface is intentionally small: two result functions and
# the shared result type. Adding to this list is a deliberate act — it
# means a new consumer needs the name — and should be paired with tests
# and a mention in the module docstring.
__all__ = [
    "ValidatedInputs",
    "validate_blueprint_document",
    "validate_blueprint_inputs",
]
