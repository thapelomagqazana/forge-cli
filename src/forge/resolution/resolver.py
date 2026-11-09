"""Variable resolution — the single boundary between user input and rendering.

Public contract
---------------
    resolve(blueprint, cli_args, prompt_answers)
        -> Result[ResolvedValues, list[ValidationError]]

Guarantees
----------
* **Deterministic.** Same inputs, same output. No I/O, no prompts, no
  clock, no environment lookups. This makes the module trivially testable
  and safe to call from async contexts, workers, and future HTTP handlers.
* **Total failure on any error.** We never return a *partially* validated
  mapping. A partial mapping is exactly the kind of object that slips
  past code review and later crashes the renderer at runtime. If any
  variable fails to resolve or validate, the caller gets `Err(errors)`
  with the complete list — never `Ok` with holes.
* **Immutable output.** `ResolvedValues` is frozen and backed by a
  `MappingProxyType`, so downstream code cannot mutate a "validated"
  mapping and thereby bypass validation.
* **No side effects on inputs.** Callers' dicts are copied before use, so
  resolve() can be called repeatedly with the same mappings without any
  cross-contamination.

Design rationale
----------------
Resolution is split into two cooperating pieces:

1. **Selection** (`precedence.choose`) — *who wins* for a given variable
   given the ordered sources. Pure data-in/data-out; no validation.
2. **Validation** (`validators.validate_variable`) — *is the winner
   legal* for the variable's declared type and rules.

Keeping those concerns apart means failure messages are precise
("required variable has no value" vs. "value fails pattern") and each
piece can be tested in isolation. The resolver is the thin orchestration
layer that combines them and packages the result.

Why a dedicated `ResolvedValues` type and not a `dict[str, Any]`?
    Downstream APIs (rendering, generation) can annotate parameters as
    `ResolvedValues` and mypy will reject any call site that passes a
    plain dict. That turns "no unvalidated input reaches the renderer"
    from a review checklist item into a compile-time guarantee.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from forge.loading.errors import ValidationError
from forge.loading.result import Err, Ok, Result
from forge.models import Blueprint, VariableDefinition, VariableType
from forge.resolution.precedence import choose
from forge.resolution.validators import validate_variable


@dataclass(frozen=True, slots=True)
class ResolvedValues(Mapping[str, Any]):
    """Immutable, resolver-owned mapping of validated variable values.

    Implements `collections.abc.Mapping` so it behaves like a dict for
    read access (`values["k"]`, `values.get(...)`, iteration, `len`,
    `in`). Mutation is not offered — use `.as_dict()` if a plain dict is
    genuinely needed (e.g. passing into a template renderer that copies).

    Construction is intentionally awkward: only the resolver (and its
    tests) construct instances directly, from a mapping it already owns.
    There is no public `from_dict`, no `of`, no auto-coercion helper —
    because every such helper is a way for unvalidated data to sneak in.
    """

    _values: Mapping[str, Any]

    # -- Mapping protocol --------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    # -- Convenience -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return a shallow copy as a plain dict.

        Used at the renderer boundary (template engines want a dict). The
        copy is deliberate: the renderer must not be able to mutate the
        validated mapping even indirectly.
        """
        return dict(self._values)

    def __repr__(self) -> str:
        # Redact values to avoid leaking secrets into logs if a variable
        # is later used for tokens or credentials.
        keys = ", ".join(sorted(self._values))
        return f"ResolvedValues(keys=[{keys}])"


class VariableResolver:
    """Resolve and validate every variable a blueprint declares.

    Stateless by design. A module-level singleton is exposed at the
    bottom of this file as `resolve_variables`; the class exists so that
    future strategies (caching, alternate precedence) can be injected
    without changing call sites.
    """

    def resolve(
        self,
        blueprint: Blueprint,
        cli_args: Mapping[str, Any] | None = None,
        prompt_answers: Mapping[str, Any] | None = None,
    ) -> Result[ResolvedValues, list[ValidationError]]:
        """Return every variable's validated value, or every error found.

        Parameters
        ----------
        blueprint
            The parsed blueprint whose `variables` list defines the rules.
        cli_args
            Raw values coming from CLI flags / `--var k=v`. Highest
            precedence. May be `None` (treated as empty).
        prompt_answers
            Raw values coming from the interactive wizard. Middle
            precedence. May be `None` (treated as empty).

        Returns
        -------
        `Ok(ResolvedValues)` when every required variable is present and
        every provided value is valid. `Err(list[ValidationError])`
        otherwise, with *all* problems aggregated.
        """
        # Copy inputs so callers can reuse their dicts and so we never
        # alias state. Cheap for the size of a typical blueprint.
        cli = dict(cli_args or {})
        prompts = dict(prompt_answers or {})

        errors: list[ValidationError] = []
        resolved: dict[str, Any] = {}

        for var in blueprint.variables:
            self._resolve_one(var, cli, prompts, resolved, errors)

        if errors:
            # Sort errors for deterministic output: by path, then by code.
            # Determinism makes CLI output diffable and tests stable.
            errors.sort(key=lambda e: (e.path, e.code))
            return Err(errors)

        # Wrap in MappingProxyType so even the resolver-owned dict cannot
        # be mutated after this point. Belt and braces with `frozen=True`.
        return Ok(ResolvedValues(MappingProxyType(resolved)))

    # -- internals ---------------------------------------------------------

    def _resolve_one(
        self,
        var: VariableDefinition,
        cli: Mapping[str, Any],
        prompts: Mapping[str, Any],
        resolved: dict[str, Any],
        errors: list[ValidationError],
    ) -> None:
        """Resolve a single variable, writing to `resolved` or `errors`.

        Kept as a small method rather than an inline loop body so that
        future enhancements (e.g. derived variables, per-variable
        deprecation warnings) have an obvious insertion point without
        rewriting the top-level flow.
        """
        path = f"variables.{var.id}"
        chosen = choose(var, cli_values=cli, prompt_values=prompts)

        if chosen is None:
            if var.required:
                errors.append(
                    ValidationError(
                        path=path,
                        message=f"required variable '{var.id}' has no value",
                        hint=self._missing_hint(var),
                        source="resolution",
                        code="required.missing",
                        extra={"variable": var.id, "type": var.type.value},
                    )
                )
            # Optional + absent + no default: intentionally *omit* from
            # resolved values. Templates can then use `value is defined`
            # / `{% if value %}` cleanly, instead of receiving a None
            # that shadows a legitimate default of None.
            return

        result = validate_variable(
            var,
            chosen.value,
            path=path,
            source=chosen.source,
        )
        if isinstance(result, ValidationError):
            errors.append(result)
            return

        resolved[var.id] = result

    def _missing_hint(self, var: VariableDefinition) -> str:
        """Produce a hint tailored to the variable's declared type.

        Generic hints ("provide a value") are useless. Users fix problems
        faster when the hint names the exact syntax that would satisfy
        the variable — especially for booleans and enums, whose accepted
        values aren't obvious from the type alone.
        """
        if var.type is VariableType.boolean:
            return (
                f"Pass --var {var.id}=true|false, or set a default "
                f"in the blueprint's variable definition."
            )
        if var.type is VariableType.enum:
            allowed = list(var.validation.enum) if var.validation and var.validation.enum else []
            joined = ", ".join(allowed) if allowed else "<blueprint has no allowed values>"
            return f"Pass --var {var.id}=<one of: {joined}>."
        return f"Pass --var {var.id}=<value>, or set a default in the blueprint."


# Module-level convenience instance. Safe because the resolver is
# stateless; provided so call sites can `from ... import resolve_variables`
# without instantiating the class.
_default_resolver = VariableResolver()


def resolve_variables(
    blueprint: Blueprint,
    cli_args: Mapping[str, Any] | None = None,
    prompt_answers: Mapping[str, Any] | None = None,
) -> Result[ResolvedValues, list[ValidationError]]:
    """One-liner wrapper around `VariableResolver().resolve()`.

    Exists purely for ergonomics. If a future strategy needs to be
    injectable, callers that use the class directly are unaffected; those
    that use this wrapper get the default behaviour.
    """
    return _default_resolver.resolve(
        blueprint,
        cli_args=cli_args,
        prompt_answers=prompt_answers,
    )
