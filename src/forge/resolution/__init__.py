"""Public façade for variable resolution.

The typed alias `ResolutionResult` keeps callers honest: mypy forces
them to handle the `Err` branch before touching `Ok`.
"""

from __future__ import annotations

from forge.loading.errors import ValidationError
from forge.loading.result import Err, Ok
from forge.resolution.resolver import ResolvedValues, VariableResolver

ResolutionResult = Ok[ResolvedValues] | Err[list[ValidationError]]

__all__ = [
    "Err",
    "Ok",
    "ResolutionResult",
    "ResolvedValues",
    "VariableResolver",
]
