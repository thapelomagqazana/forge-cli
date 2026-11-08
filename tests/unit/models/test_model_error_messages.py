"""Error-message tests: rejections must be actionable, not opaque.

What this test is *for*
-----------------------
``test_schema_model_parity.py`` proves the two engines agree on
*which* documents they accept. It says nothing about *why* a rejected
document was rejected. That is a different contract, and it is the one
a user actually reads when ``forge create`` fails.

``validate_blueprint_document`` runs schema-first, model-second (see
``forge.verification.handlers``). That ordering means a document that
fails the schema never reaches the model — so the exception a caller
sees depends on which engine caught the violation:

* **Schema-detectable violations** (missing required field, wrong
  type, `type="enum"` without `validation.enum`, `type="command"`
  without `command`, `type="command"` *with* `path`, etc.) raise
  ``SchemaValidationError`` — exit code 1, "this is a user error,
  fix your blueprint".
* **Model-only violations** (a `boolean` variable whose `default` is
  a string; an `enum` variable whose `default` is not one of its
  choices, etc.) raise ``ModelValidationError`` — exit code 5, "this
  is a Forge bug, report it".

Both types carry a structured ``(pointer, message)`` list. This file
pins the parts of that list a human and a script rely on:

1. **The exception type matches the engine.** A schema-detectable
   violation must raise ``SchemaValidationError`` (user error, exit
   1); a model-only violation must raise ``ModelValidationError``
   (Forge bug, exit 5). Getting this classification wrong misroutes
   the failure to the wrong exit code, which is what scripts branch
   on.

2. **Every ``errors`` entry is a ``(pointer, message)`` tuple.** The
   pointer is an RFC 6901 JSON Pointer, the message is human-readable
   prose. Both must be present and non-empty; a regression that lost
   the pointer (returning ``""`` for every error) would still reject
   the document but would leave a script with no way to branch on the
   location of the failure.

3. **The pointer names the *specific* offending value when the engine
   can name it.** The schema reports the parent for ``required`` and
   ``additionalProperties`` violations (it has no more specific
   location); the model reports the specific field when its
   validators fire. Both are correct at their level. The tests below
   assert the pointer that the engine *that actually caught the
   violation* produces, not the pointer the *other* engine would
   produce for a hypothetical variant of the same document.

4. **The ``__str__`` renders one bullet per violation.** This is the
   only guarantee about the rendered output — the exact prose is
   deliberately *not* asserted, because Pydantic and jsonschema both
   reword messages across releases. Asserting on prose would make
   this file a permanent source of noisy, non-semantic failures.

What this test is *not* for
---------------------------
* **Exhaustive pointer correctness.** That is the schema-side pointer
  test's job (``test_schema_rejects.py``) and the parity test's job
  (which catches pointer divergence between engines). This file
  asserts the *shape* of the error surface, not the full truth table
  of every possible pointer.

* **Triggering every possible rejection.** The cases here are ones a
  user can plausibly produce by hand-writing a blueprint. Rare
  conditions (e.g. a corrupted string in a pattern) are covered by
  the parity test's mutation strategies, not by hand-written cases.

* **Message wording.** See above.

Design notes
------------
* **Documents are constructed in-test, not loaded from fixtures.**
  Each case is a *narrow* mutation of a known-good baseline, and the
  test's readability depends on the mutation being visible next to
  the assertion. Fixture-based variants would force the reader to
  open two files to understand one test.

* **The baseline is a function, not a constant.** ``_baseline()``
  returns a fresh dict on each call so mutations in one test cannot
  leak into another. A module-level constant would be mutated by the
  first test that mutates it, and every subsequent test would see
  the mutation — a classic pytest trap that produces order-dependent
  failures.

* **Cases are split by engine.** ``_schema_caught_cases()`` and
  ``_model_caught_cases()`` are separate helpers because the two
  engines produce different exception types and (for
  ``required``-style violations) different pointer specificities.
  Splitting them keeps the expected type and the expected pointer
  consistent within each case, which is what makes the test
  meaningful when it fails.

* **Every rule the schema can express belongs to the schema.** A
  case that *used* to be model-caught because the schema was silent
  moves to ``_schema_caught_cases()`` when the schema grows a rule
  for it. The ``command-check-with-path`` case is the canonical
  example: originally the model rejected it (as a copy-paste
  mistake) while the schema accepted it, which classified the
  failure as a Forge bug (exit 5) even though it was a user error.
  The schema now has the matching ``allOf``/``not`` rule, so the
  case is user-caught (exit 1) and the classification is honest.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.core.exceptions import ModelValidationError, SchemaValidationError
from forge.models import Blueprint
from forge.verification import validate_blueprint_document


# ---------------------------------------------------------------------------
# Baseline document
# ---------------------------------------------------------------------------
def _baseline() -> dict[str, object]:
    """Return a minimal, valid blueprint document.

    Kept as a function so each test gets a fresh, independent copy.
    The baseline is deliberately the *smallest* valid document — every
    test then mutates exactly one field, which makes the resulting
    error's pointer and message unambiguous. A larger baseline would
    mean a mutation could interact with an existing rule (e.g.
    changing ``verification`` when a conditional-file rule happens to
    reference it) and the test would report a pointer that is not the
    one the author intended to assert.

    The document omits ``variables``, ``files``, ``conditional_files``,
    and ``maintainer`` on purpose: all four have schema defaults, are
    not required, and their presence in the baseline would make the
    failing pointers for the mutated cases noisier than they need to
    be.
    """
    return {
        "schema_version": "1.0.0",
        "id": "sample.demo",
        "name": "Demo Blueprint",
        "version": "1.0.0",
        "description": "A minimal blueprint used as a test baseline.",
        "category": "api",
        "verification": [
            {"id": "readme", "type": "required_file", "path": "README.md"},
        ],
        "compatibility": {"min_forge_version": "1.0.0"},
    }


# ---------------------------------------------------------------------------
# Cases the *schema* catches (schema-first funnel → SchemaValidationError)
# ---------------------------------------------------------------------------
def _schema_caught_cases() -> list[tuple[str, dict[str, object], str]]:
    """Return ``(case_id, document, expected_pointer)`` for schema violations.

    These are cases where the schema has an explicit rule (``allOf``/
    ``if-then`` or a ``required`` declaration) that fires on the
    document. Because the funnel runs schema-first, the schema
    catches them before the model's equivalent ``model_validator`` can
    run — so the caller sees ``SchemaValidationError``, not
    ``ModelValidationError``.

    That classification is not a testing artifact: it is the design.
    ADR-0007 makes the schema the referee, and ADR-0008 makes the
    model a projection of it. If the schema can name the violation,
    the schema should be the engine that rejects it, and the exit code
    should be 1 (user error). The model's ``model_validator`` for the
    same rule exists to keep the two engines' accept-sets equal, not
    to be the primary error surface.

    **Pointer specificity.** ``_format_pointer`` in
    ``forge.verification.handlers`` post-processes ``required`` and
    ``additionalProperties`` messages to append the offending field
    name, because jsonschema's raw path for those keywords is the
    *parent object* rather than the missing field. That post-processing
    applies wherever the keyword fires, including inside an
    ``if/then`` branch — so a violation of ``"required": ["command"]``
    inside a ``then`` branch produces ``/verification/0/command``, not
    ``/verification/0``. That is more specific and more useful than
    the parent pointer, and the expectations below reflect it.
    """
    cases: list[tuple[str, dict[str, object], str]] = []

    # --- Variable type: enum without validation.enum ---------------------
    # The schema's ``if/then`` for ``type="enum"`` has two ``required``
    # clauses: ``validation`` must exist, and if it exists ``enum``
    # must exist. Only the first can fire when ``validation`` is
    # missing entirely, so the pointer names the missing field itself:
    # ``/variables/0/validation``.
    enum_no_choices = _baseline()
    enum_no_choices["variables"] = [
        {
            "id": "mode",
            "type": "enum",
            "description": "Which mode to use.",
            "required": True,
            # Deliberately omits ``validation``.
        }
    ]
    cases.append(("enum-variable-without-validation", enum_no_choices, "/variables/0/validation"))

    # --- Variable type: enum with empty validation.enum ------------------
    # ``validation.enum`` is present but has length 0, so the
    # ``minItems: 1`` rule fires at the list itself.
    enum_empty = _baseline()
    enum_empty["variables"] = [
        {
            "id": "mode",
            "type": "enum",
            "description": "Which mode to use.",
            "required": True,
            "validation": {"enum": []},
        }
    ]
    cases.append(("enum-variable-with-empty-choices", enum_empty, "/variables/0/validation/enum"))

    # --- Check: command type without command field -----------------------
    # The schema's ``if/then`` declares ``required: ["command"]``
    # when ``type == "command"``. ``_format_pointer`` appends the
    # missing field name, so the pointer names the specific absent
    # field: ``/verification/0/command``.
    command_missing = _baseline()
    command_missing["verification"] = [
        {"id": "run", "type": "command"},
    ]
    cases.append(("command-check-without-command", command_missing, "/verification/0/command"))

    # --- Check: required_file type without path field --------------------
    # Mirror of the case above, for the other branch of the schema's
    # ``if/then`` pair.
    path_missing = _baseline()
    path_missing["verification"] = [
        {"id": "check", "type": "required_file"},
    ]
    cases.append(("required-file-check-without-path", path_missing, "/verification/0/path"))

    # --- Check: command type with a path field present -------------------
    # The schema's ``allOf`` includes a negative branch:
    #
    #     if   type == "command"
    #     then path must not be present
    #
    # (encoded as ``"not": {"required": ["path"]}``, which is JSON
    # Schema's idiom for "forbidden"). The pointer names the whole
    # check object because the ``not`` clause operates on the object,
    # not on a specific field — there is no missing field to name.
    #
    # Classification note: this case *used to be* model-caught (the
    # schema was silent and the model's ``_enforce_discriminated_payload``
    # rejected it). When the schema grew the matching rule, the case
    # moved here — the failure is now a user error (exit 1) rather
    # than a Forge bug (exit 5), which is the correct advice: a
    # ``command`` check with a ``path`` is a copy-paste mistake by the
    # blueprint author, not a defect in Forge.
    command_with_path = _baseline()
    command_with_path["verification"] = [
        {"id": "run", "type": "command", "command": "pytest", "path": "README.md"},
    ]
    cases.append(("command-check-with-path", command_with_path, "/verification/0"))

    return cases


# ---------------------------------------------------------------------------
# Cases the *model* catches (schema accepts → ModelValidationError)
# ---------------------------------------------------------------------------
def _model_caught_cases() -> list[tuple[str, dict[str, object], str]]:
    """Return ``(case_id, document, expected_pointer)`` for model-only violations.

    These are cases the schema *permits* but the model rejects. They
    are the reason ``ModelValidationError`` exists at all: the schema
    is silent on the rule, so the model is the only engine that can
    catch it. If either of these cases starts raising
    ``SchemaValidationError``, the schema has been tightened
    (deliberately or accidentally) and the classification of these
    tests must be revisited.

    The expected pointer is what the *model* engine produces. For a
    ``model_validator(mode="after")``, that is the model instance
    (e.g. ``/variables/0``), not the specific field the validator
    complains about — Pydantic does not support sub-field pointers
    from a ``model_validator``. The tests assert the pointer Pydantic
    actually produces; a ``field_validator`` could produce a more
    specific pointer, but the rules here are cross-field by nature
    and belong on the model, not on a single field.
    """
    cases: list[tuple[str, dict[str, object], str]] = []

    # --- Variable default kind mismatch ----------------------------------
    # The schema types ``default`` as ``Any`` and does not express
    # "kind must match ``type``". The model's
    # ``_enforce_default_matches_type`` validator enforces it and
    # points at the variable (the model it belongs to) rather than
    # ``/variables/0/default`` — a limitation of
    # ``model_validator(mode="after")``, not a bug in the funnel.
    default_kind = _baseline()
    default_kind["variables"] = [
        {
            "id": "enabled",
            "type": "boolean",
            "description": "Whether the feature is enabled.",
            "required": False,
            "default": "yes",  # string, not bool
        }
    ]
    cases.append(("boolean-default-is-string", default_kind, "/variables/0"))

    # --- Variable default not in enum list -------------------------------
    # The schema does not express "``default`` must be one of
    # ``validation.enum``". The model does. The pointer is the
    # variable, for the same ``model_validator`` reason as above.
    default_not_in_enum = _baseline()
    default_not_in_enum["variables"] = [
        {
            "id": "mode",
            "type": "enum",
            "description": "Which mode to use.",
            "required": True,
            "default": "invalid",
            "validation": {"enum": ["fast", "slow"]},
        }
    ]
    cases.append(("enum-default-not-in-choices", default_not_in_enum, "/variables/0"))

    return cases


# ---------------------------------------------------------------------------
# Schema-caught: exception type and structure
# ---------------------------------------------------------------------------
@pytest.mark.unit()
@pytest.mark.parametrize(
    ("case_id", "document", "expected_pointer"),
    _schema_caught_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_schema_caught_rejection_raises_schema_validation_error(
    case_id: str,
    document: dict[str, object],
    expected_pointer: str,
) -> None:
    """Schema-detectable violations raise ``SchemaValidationError`` (exit 1).

    Asserting the specific type — not ``ForgeError``, not ``Exception``
    — is what makes the CLI's exit-code classification predictable
    from the funnel's behaviour. A regression that returned
    ``ModelValidationError`` here would misroute a user error to exit
    5 and tell the user to report a bug that is actually theirs.
    """
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_blueprint_document(document, source=case_id)

    error = exc_info.value

    # The funnel always tags errors with the caller's ``source`` label.
    # A regression that dropped the source would make the error's
    # message ("Blueprint '<source>' failed...") meaningless — the
    # user would not know which file to fix.
    assert error.source == case_id

    # A ``SchemaValidationError`` without errors is a contradiction:
    # the whole point of the type is to carry the aggregated error
    # list. The funnel only constructs the error when the validator
    # produced at least one error.
    assert error.errors, (
        f"SchemaValidationError for {case_id!r} carried no errors; "
        "the funnel must have swallowed the validator output"
    )


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("case_id", "document", "expected_pointer"),
    _schema_caught_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_schema_caught_pointer_names_the_offending_value(
    case_id: str,
    document: dict[str, object],
    expected_pointer: str,
) -> None:
    """Each schema rejection's pointer must name the offending value.

    The pointer is asserted *as a member of* the error list, not as
    ``errors[0][0]``. Some cases legitimately produce more than one
    error (the schema aggregates), and asserting on the first would
    make the test depend on error ordering — which is not a stable
    part of the contract. The membership assertion captures the
    actual requirement: "the pointer I care about is in the list."
    """
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_blueprint_document(document, source=case_id)

    pointers = [pointer for pointer, _message in exc_info.value.errors]
    assert expected_pointer in pointers, (
        f"expected pointer {expected_pointer!r} for case {case_id!r}; " f"got pointers {pointers!r}"
    )


# ---------------------------------------------------------------------------
# Model-caught: exception type and structure
# ---------------------------------------------------------------------------
@pytest.mark.unit()
@pytest.mark.parametrize(
    ("case_id", "document", "expected_pointer"),
    _model_caught_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_model_caught_rejection_raises_model_validation_error(
    case_id: str,
    document: dict[str, object],
    expected_pointer: str,
) -> None:
    """Model-only violations raise ``ModelValidationError`` (exit 5).

    These are the cases where the schema is silent and the model is
    the sole engine that can reject the document. Getting the
    exception type right is what tells the user "this is a Forge bug,
    not your mistake" — the opposite message to the schema-caught
    cases above.
    """
    with pytest.raises(ModelValidationError) as exc_info:
        validate_blueprint_document(document, source=case_id)

    error = exc_info.value

    assert error.source == case_id

    assert error.errors, (
        f"ModelValidationError for {case_id!r} carried no errors; "
        "the funnel must have swallowed the underlying ValidationError"
    )


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("case_id", "document", "expected_pointer"),
    _model_caught_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_model_caught_pointer_names_the_offending_value(
    case_id: str,
    document: dict[str, object],
    expected_pointer: str,
) -> None:
    """Each model rejection's pointer names the value the model complained about.

    Pydantic's ``model_validator(mode="after")`` raises on the model
    instance, not on a sub-field, so the pointer it produces is the
    *model's* location (e.g. ``/variables/0`` for a variable-level
    rule), not the specific field (``/variables/0/default``). This is
    a limitation of Pydantic's ``model_validator``, not of the
    funnel — the funnel faithfully preserves the ``loc`` tuples
    Pydantic provides. The test asserts the pointer Pydantic actually
    produces, not the pointer a ``field_validator`` would produce,
    because the code uses ``model_validator`` by design (the rule is
    cross-field, not per-field).
    """
    with pytest.raises(ModelValidationError) as exc_info:
        validate_blueprint_document(document, source=case_id)

    pointers = [pointer for pointer, _message in exc_info.value.errors]
    assert expected_pointer in pointers, (
        f"expected pointer {expected_pointer!r} for case {case_id!r}; " f"got pointers {pointers!r}"
    )


# ---------------------------------------------------------------------------
# Rendered message shape
# ---------------------------------------------------------------------------
@pytest.mark.unit()
@pytest.mark.parametrize(
    ("error_class", "case_id", "document", "header_word"),
    [
        (
            SchemaValidationError,
            "enum-without-validation",
            _schema_caught_cases()[0][1],
            "schema",
        ),
        (
            ModelValidationError,
            "boolean-default-is-string",
            _model_caught_cases()[0][1],
            "model",
        ),
    ],
)
def test_rendered_message_lists_one_bullet_per_violation(
    error_class: type,
    case_id: str,
    document: dict[str, object],
    header_word: str,
) -> None:
    """``str(error)`` renders every violation on its own line, prefixed with a bullet.

    The test covers *both* error types, because the CLI renders both
    through the same handler and the user sees both through the same
    terminal. The shape guarantee is the same for both:

    1. The first line names the source and says "failed <engine>
       validation".
    2. Each subsequent line starts with the bullet and contains both
       the pointer and the message.

    The exact prose after the bullet is deliberately *not* asserted,
    because both jsonschema and Pydantic reword messages across
    releases. What matters is the structure: a header, then a
    scannable list where "where" and "what" are visible side by side.

    Parameterising over both types is deliberate: a regression that
    dropped the bullet from ``ModelValidationError.__str__`` (which
    overrides ``StructuredError.__str__``) would pass a
    schema-only test. This parameterisation catches that.
    """
    with pytest.raises(error_class) as exc_info:  # type: ignore[misc]
        validate_blueprint_document(document, source=case_id)

    rendered = str(exc_info.value)
    lines = rendered.splitlines()

    # First line: the header. Asserting on the presence of both the
    # source name and the engine word ("schema" or "model") is what
    # distinguishes the two error types at a glance — a user
    # debugging a blueprint should not have to read the whole message
    # to learn which validator fired.
    assert case_id in lines[0]
    assert header_word in lines[0].lower()

    # Bullet lines: one per error, each carrying a pointer and a message.
    bullet_lines = [line for line in lines if line.lstrip().startswith("•")]
    assert len(bullet_lines) == len(exc_info.value.errors), (
        f"expected one bullet per error ({len(exc_info.value.errors)}), " f"got {len(bullet_lines)}"
    )

    # Every bullet line must contain a JSON Pointer. The pointer is
    # rendered as ``<pointer>`` or ``/<pointer>`` — the leading slash
    # is what distinguishes a pointer from prose. Checking that *some*
    # slash is present is enough to catch a regression that dropped
    # the pointer from the rendered output; checking the exact pointer
    # is the parameterised tests' job.
    for line in bullet_lines:
        assert "/" in line, f"bullet line does not contain a JSON Pointer: {line!r}"


# ---------------------------------------------------------------------------
# Bug-report hint
# ---------------------------------------------------------------------------
@pytest.mark.unit()
def test_model_error_hint_points_at_parity_check() -> None:
    """``ModelValidationError``'s hint must tell the user this is a Forge bug.

    ``ModelValidationError`` is classified as an internal error (exit
    5) because a schema-valid document that the model rejects is a
    parity regression (ADR-0008). The hint is the only place the
    user is told *what to do about it* — and the correct advice is
    "report this", not "fix your blueprint". A hint that pointed at
    ``SCHEMA_REFERENCE.md`` (the schema error's hint) would send the
    user to the wrong document, which is the single most expensive
    mistake an error message can make.

    The assertion is on a substring (``models-check``) rather than
    exact prose because the hint is prose. The substring is chosen
    because it names the exact command that catches the class of
    bug, which is what makes the hint actionable rather than merely
    apologetic.
    """
    document = _baseline()
    document["variables"] = [
        {
            "id": "enabled",
            "type": "boolean",
            "description": "Whether the feature is enabled.",
            "required": False,
            "default": "yes",
        }
    ]

    with pytest.raises(ModelValidationError) as exc_info:
        validate_blueprint_document(document, source="hint-case")

    hint = exc_info.value.hint
    assert hint is not None, "ModelValidationError must carry a hint"
    # The hint must be *specific* — "report a bug" alone would be
    # useless, because the user does not know which bug or where to
    # look. Naming the regeneration check gives them a concrete first
    # step they can take before filing an issue, which often resolves
    # the problem (a stale model file is the most common cause).
    assert "models-check" in hint


# ---------------------------------------------------------------------------
# Direct model construction (bypassing the funnel) still surfaces errors
# ---------------------------------------------------------------------------
@pytest.mark.unit()
def test_direct_model_validation_raises_pydantic_validation_error() -> None:
    """Calling ``Blueprint.model_validate`` directly skips the wrapper.

    Some callers — notably the parity test — deliberately bypass the
    funnel so they can observe Pydantic's native error shape without
    the wrapper's translation. That is a legitimate use, and this
    test pins it: the model itself, not just the funnel, rejects an
    invalid document. If a future refactor made the model's
    validators lazy (e.g. by deferring validation to the funnel), the
    parity test would start seeing acceptance where it should see
    rejection, and the parity test's own failure would not make the
    cause obvious — this test makes it obvious.

    The assertion is on ``pydantic.ValidationError`` specifically,
    not ``Exception``. That is the documented exception type for
    ``model_validate`` failures; anything else would be a bug in the
    model or in Pydantic, and would deserve to fail loudly rather
    than be masked by a broad assertion.
    """
    document = _baseline()
    document["variables"] = [
        {
            "id": "mode",
            "type": "enum",
            "description": "Which mode to use.",
            "required": True,
        }
    ]

    with pytest.raises(ValidationError):
        Blueprint.model_validate(document)
