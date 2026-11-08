"""Safe YAML parsing with precise, located failure information.

Scope of this module
--------------------
Exactly one job: turn `bytes` into either a validated `dict` or a
`ValidationError` that names *where* the parse failed (line and column)
and *what* to do about it. It does not validate against the schema, does
not construct the Pydantic model, and does not touch the filesystem.

Keeping the scope narrow is deliberate. YAML parsing is where every
byte-level hazard lives (encoding, BOMs, tabs, aliases, custom tags,
duplicate keys), and concentrating that surface here means the schema and
model phases only ever see clean, typed Python data. It also means every
YAML-related failure mode can be enumerated and tested in one file.

Threat model
------------
`safe_load` (not `load`) is mandatory: PyYAML's `load` can instantiate
arbitrary Python objects via `!!python/object` tags, which is the classic
RCE-in-a-config-file footgun. `safe_load` restricts the constructor table
to built-in scalar types, lists, and dicts. This module additionally:

* Rejects non-UTF-8 input at the decode boundary rather than letting
  PyYAML's internal codec guess (which may succeed silently on some
  inputs and produce mojibake elsewhere).
* Rejects a document root that is not a mapping — a blueprint is a map
  by contract, and a top-level list or scalar can only be a user error
  worth reporting immediately with an actionable hint.
* Never constructs objects or executes anything from the document.

Failure modes and their disposition
-----------------------------------
1. **Non-UTF-8 bytes** → `ValidationError` at the decode boundary with
   a hint to re-save as UTF-8. (Line/column unknown.)
2. **YAML syntax error** → `ValidationError` with the parser's reported
   line/column. This is where most user edits go wrong (tabs, unquoted
   colons, mismatched indentation).
3. **Non-mapping root** → `ValidationError` at `<root>` explaining that
   the top of the file must be a mapping.
4. **`null` document** (empty file or only comments) → the same
   non-mapping error, with a hint to add the required fields.

Cases 1 to 4 all surface as a single `ValidationError`, never an exception,
because the loader's contract is that every phase returns values.

Why return a union instead of raising
-------------------------------------
The loader aggregates errors across phases. If parsing raised, the loader
would need `try/except` around phase 1, breaking the uniform "call phase,
check return, extend list" shape for every other phase. Returning a union
keeps the loader's flow flat and lets the parser be tested without any
exception-plumbing. Callers narrow with `isinstance(parsed, ValidationError)`.

Why dataclasses for the success case
------------------------------------
A `ParsedDocument` (rather than a bare `dict`) carries two useful pieces:

* `data` — the parsed mapping.
* `source` — the caller-supplied label, propagated so downstream phases
  (schema, model, filesystem) can stamp their own `ValidationError`s
  with the same source without re-plumbing it through every signature.

Making it frozen and slotted keeps it cheap to construct (one per load)
and prevents a phase from accidentally mutating the parsed document,
which would make errors non-reproducible.

Not done here, on purpose
-------------------------
* **Duplicate-key detection.** PyYAML's default behaviour is
  "last-key-wins", which silently discards earlier duplicate keys. We
  *could* detect duplicates via a custom `Loader`, and it would be a
  genuine improvement — but it is orthogonal to location-aware parsing
  and can be layered on later without changing this module's API
  (still returns `ParsedDocument | ValidationError`). Adding it now would
  grow this file's surface and its tests without a concrete consumer.
  Logged as future work in `docs/architecture/loader.md`.
* **BOM stripping.** PyYAML handles a UTF-8 BOM transparently. Stripping
  it here would be redundant and could confuse a future caller that wants
  to know the exact byte layout of the file.
* **Anchor/alias expansion limits.** PyYAML's "billion laughs" protection
  is adequate for the sizes of document we process (config files, not
  arbitrary input). If loader input ever becomes user-uploaded, revisit.

Testing
-------
Covered by `tests/unit/loading/test_yaml_parser.py`:
    * valid document returns `ParsedDocument` with matching `source`;
    * malformed YAML returns located `ValidationError` with `line >= 1`;
    * non-mapping root returns a rooted error with a hint;
    * empty document returns a rooted error with a hint;
    * non-UTF-8 input returns a decode error with a hint;
    * every returned `ValidationError` has a non-empty `hint`
      (guaranteed by the shared catalogue in `loading/hints.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from forge.loading.errors import ValidationError
from forge.loading.hints import hint_for


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """A successfully parsed blueprint document.

    Frozen so downstream phases cannot accidentally mutate the source of
    truth — any derived value (model, template list, rendered output)
    must be produced from `data`, not written back into it.
    """

    data: dict[str, Any]
    source: str


def parse_yaml(raw: bytes, *, source: str) -> ParsedDocument | ValidationError:
    """Parse `raw` as a YAML mapping.

    Parameters
    ----------
    raw
        The file's raw bytes. Decoding is done here so encoding errors
        are reported at the same layer as syntax errors — from the
        caller's perspective, both are "cannot read this file as YAML".
    source
        Label identifying the input (usually a file path or `<memory>`).
        Stamped onto any `ValidationError` we return so the loader can
        render a complete message without knowing about this module.

    Returns
    -------
    ParsedDocument
        On success: a mapping plus the source label.
    ValidationError
        On any failure. Never raises for expected input problems; raises
        only for programmer errors (none are currently defined).

    Notes
    -----
    Order of checks matters:

    1. Decode first, then parse. This ensures a non-UTF-8 file gets a
       clear "encoding" error rather than a confusing YAML "found
       unexpected byte" error that does not name the actual problem.
    2. Parse with `safe_load`, so no tags can execute.
    3. Reject non-mappings last, once we know the input is well-formed
       YAML — so a top-level list is reported as "root must be a
       mapping", not as a syntax error.
    """

    # ------------------------------------------------------------------
    # Phase A — decode UTF-8.
    #
    # Bytes in, text out. A `UnicodeDecodeError` names the exact byte
    # offset (which we do not surface — byte offsets are useless to YAML
    # authors) and the offending byte. We report the byte's *reason*
    # (`invalid start byte`, `unexpected end of data`, ...) but not its
    # position, because editors cannot jump to a byte offset.
    #
    # `strict` is the default and is the correct choice: `surrogateescape`
    # would let non-UTF-8 files through, producing mojibake that surfaces
    # as an opaque schema error three phases later. Failing early here
    # gives the user the exact fix ("re-save as UTF-8") at the exact
    # moment it becomes relevant.
    # ------------------------------------------------------------------
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ValidationError(
            path="",
            message=f"file is not valid UTF-8: {exc.reason}",
            hint=(
                "Re-save the blueprint as UTF-8. Most editors have a "
                "'Save with encoding' option; if you cannot, run "
                "`iconv -f <current-encoding> -t UTF-8 file.yaml`."
            ),
            source=source,
        )

    # ------------------------------------------------------------------
    # Phase B — parse with `safe_load`.
    #
    # `safe_load` restricts the loader's constructor table to a fixed
    # set of built-in scalar/list/map types. Custom tags like
    # `!!python/object/apply:os.system` raise `ConstructorError`, which
    # subclasses `YAMLError` and is caught below — turning a would-be
    # RCE into a validation error the user can act on.
    #
    # Empty documents (`""`, only comments, only `---`) parse to `None`,
    # which we reject in Phase C with a friendly "root must be a mapping"
    # message — more actionable than a bare "expected mapping, got None".
    # ------------------------------------------------------------------
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # `YAMLError` covers three families:
        #   * `ScannerError`    — lexical problems (bad characters, tabs)
        #   * `ParserError`     — grammar problems (unclosed brackets, etc.)
        #   * `ConstructorError`— tags that `safe_load` refuses
        # We render each as a single located error. `problem_mark` is
        # present for scanner/parser errors; constructor errors may lack
        # it (the parser has already finished when the constructor runs),
        # so `line`/`column` stay `None` in that case — which is honest.
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        column = (mark.column + 1) if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc).strip()

        # `problem` is a short human sentence ("found unexpected ':'",
        # "expected <block end>, but found '-'"). It is exactly what we
        # want in `message`; the class name (`ScannerError`) would only
        # confuse end users.
        return ValidationError(
            path="",
            message=f"invalid YAML: {problem}",
            hint=hint_for(""),
            source=source,
            line=line,
            column=column,
        )

    # ------------------------------------------------------------------
    # Phase C — enforce mapping-root contract.
    #
    # Three distinct non-mapping outcomes collapse to one clear message:
    #   * `None`      — empty file or only comments/`---`.
    #   * `list`      — a YAML sequence at the top level.
    #   * `str`/`int` — a scalar.
    #
    # A user who wrote a list at the top level almost certainly meant to
    # nest it under a key (`variables:`, `files:`). The hint names the
    # most common fix rather than telling them "must be a mapping" and
    # leaving them stuck.
    # ------------------------------------------------------------------
    if not isinstance(data, dict):
        # `None` is worth a dedicated message: it happens with empty files
        # (a fresh `touch blueprint.yaml`) and confusion there is common.
        if data is None:
            message = "blueprint is empty or contains only comments"
            hint = (
                "A blueprint must be a YAML mapping. Start with the "
                "required fields: schema_version, id, name, version, "
                "description, category, verification, compatibility."
            )
        else:
            message = f"blueprint root must be a mapping, got {type(data).__name__}"
            hint = (
                "Wrap top-level lists or scalars under their intended key. "
                "For example, a list of variables belongs under 'variables:', "
                "not at the top of the file."
            )
        return ValidationError(
            path="",
            message=message,
            hint=hint,
            source=source,
        )

    return ParsedDocument(data=data, source=source)
