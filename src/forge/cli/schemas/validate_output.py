"""`forge validate` output DTO — the machine-readable contract for `--json`.

Why this file exists
--------------------
The `--json` output of `forge validate` is a *public contract*: CI
pipelines, IDE integrations, and blueprint-authoring tools consume it.
That contract needs to live somewhere explicit so it can be:

    * reviewed as a contract (not buried in command code),
    * version-tagged (`schema_version`) so consumers can pin behaviour,
    * snapshot-locked in tests (see tests/contract/test_validate_output_contract.py),
    * extended additively without breaking existing consumers.

Design choices
--------------
* **Frozen + `extra="forbid"`** — the DTO is immutable once built, and
  unknown fields raise at construction. This catches typos during
  development instead of silently leaking new keys into the public output.

* **Explicit `valid` field** — even though it is derivable from
  `len(errors) == 0`, emitting it explicitly means a consumer's first
  branch (`if payload["valid"]`) never has to compute anything, and an
  accidental invariant break is visible in diffs.

* **Nullable `blueprint_id` / `version`** — only meaningful when the
  blueprint is valid. Emitting `null` (rather than omitting the keys)
  keeps the envelope shape stable across success and failure — JSON
  consumers can rely on the keys *always* existing.

* **`errors[]` always present, never null** — an empty array on success
  is the canonical "nothing wrong" shape. This matches the convention of
  the loader's `Result[..., list[ValidationError]]` and keeps consumers
  from special-casing `null`.

* **`code` on each error** — a machine-readable error class
  (`required.missing`, `type.mismatch`, `input.not_found`, `tool.error`,
  …) so automation can branch without parsing prose. Defaults to `""`
  rather than `None` so the field type is a plain string.

* **`line` / `column` are optional** — only the YAML parser (and, later,
  line-aware schema extensions) can produce them. `None` means "unknown",
  which is distinct from `0` and keeps `if err.line is not None` correct.

* **`schema_version` is a `Literal`** — for v1 it is pinned to `"1.0.0"`.
  When we introduce v1.1.0 additively we change the literal and add a
  `default`; the type checker then forces every construction site to be
  revisited, which is exactly the review we want when a contract shifts.

Serialisation
-------------
Renderers call `model_dump(mode="json")` and then `json.dumps(...,
sort_keys=True, separators=(",", ":"))`. That keeps output byte-stable
across runs, which is what makes snapshot tests reliable and CI diffs
meaningful. The DTO itself stores the *values*; formatting lives in the
renderer, not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ValidateError(BaseModel):
    """One validation error in the machine-readable contract.

    Mirrors the internal `forge.loading.errors.ValidationError` but is a
    *separate* type on purpose: the internal error may grow fields that
    should not leak into the public contract (e.g. `extra` metadata).
    A future field addition here is a deliberate contract change; a
    change to the internal type is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Dotted path within the blueprint, e.g. "variables.0.type".
    # Empty string is a valid value meaning "the document as a whole".
    path: str

    # One-sentence description of the problem.
    message: str

    # Suggested fix. Required and non-empty — an error without a hint is a
    # defect in the loader, not something consumers should tolerate.
    hint: str

    # Machine-readable error class, e.g. "required.missing", "io.error".
    # Empty string when no class is known; never null.
    code: str = ""

    # Populated only when the parser can locate the failure.
    line: int | None = None
    column: int | None = None


class ValidateOutput(BaseModel):
    """Envelope returned by `forge validate --json`.

    Shape (v1.0.0):

        {
          "schema_version": "1.0.0",
          "valid": true,
          "source": "./my-blueprint",
          "blueprint_id": "python.fastapi.api",
          "version": "1.0.0",
          "errors": []
        }

    Consumers must branch on the *process exit code* first (0 valid,
    2 invalid, 1 tool error) and read this envelope second. The envelope
    is emitted for both valid and invalid blueprints so a single parser
    handles both paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Contract version. Pinned for v1; bumped when the shape changes.
    schema_version: Literal["1.0.0"] = "1.0.0"

    # First thing every consumer reads. Mirrors `len(errors) == 0` but is
    # emitted explicitly so consumers never derive it.
    valid: bool

    # The path the user supplied (directory or file). Echoed back so a
    # batch of validate invocations can be correlated unambiguously.
    source: str

    # Populated only when `valid` is True. Present (as null) on failure so
    # the envelope shape is identical across outcomes.
    blueprint_id: str | None = None
    version: str | None = None

    # Always a list, never null. Empty on success.
    errors: list[ValidateError] = Field(default_factory=list)
