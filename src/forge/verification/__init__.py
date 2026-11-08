"""Verification layer — the single funnel for validating inputs and documents.

What lives here
---------------
This package owns every rule that decides whether a *thing* is valid,
regardless of where that thing came from:

* ``validate_blueprint_document`` — checks a parsed blueprint YAML/JSON
  document against the bundled JSON Schema. Called by the blueprint
  loader, the CLI, tests, and any future importer.
* ``validate_blueprint_inputs`` — checks the *user-supplied* inputs
  (flags or wizard answers) against the blueprint's variable rules
  before scaffolding begins.

Both functions converge on the same rulebook. Adding a rule means
editing the schema or the input validator — never touching the CLI,
the loader, or the generator. That is the whole point of this package
existing as a separate layer.

Why a façade module?
--------------------
Callers should import from ``forge.verification`` and never from
``forge.verification.handlers`` directly. The handlers module is an
implementation detail: today the logic is two functions in one file;
tomorrow it may split into ``document.py`` and ``inputs.py`` without
breaking a single call site. The façade makes that refactor invisible.

Boundary discipline
-------------------
* **No I/O here.** This package does not read files, prompt users, or
  write outputs. It receives parsed data structures and either returns
  a validated result or raises a typed exception.
* **No presentation.** Error *messages* are constructed here (they are
  part of the contract), but rendering them to a stream is the CLI's
  job. This keeps verification usable from non-CLI contexts such as
  future API servers or IDE plugins.
* **Typed failures only.** Every rejection path raises a subclass of
  ``forge.core.exceptions.ForgeError`` (specifically
  ``SchemaValidationError`` or ``UserInputError``). Callers never
  receive bare ``ValueError`` or ``KeyError``, which means error
  handling is uniform across the entire application.
"""

from __future__ import annotations

from forge.verification.handlers import (
    ValidatedInputs,
    validate_blueprint_document,
    validate_blueprint_inputs,
)

__all__ = [
    "ValidatedInputs",
    "validate_blueprint_document",
    "validate_blueprint_inputs",
]
