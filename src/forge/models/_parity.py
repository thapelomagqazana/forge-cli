"""Helpers for the schema↔model parity test.

Kept out of the test file so the parity logic can be reused by future
tooling (e.g. a `forge doctor` subcommand that reports parity status).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import jsonschema
from pydantic import ValidationError

from forge.models import Blueprint
from forge.schemas import load_schema


def schema_accepts(document: Mapping[str, Any]) -> bool:
    validator = jsonschema.Draft202012Validator(load_schema())
    return not any(validator.iter_errors(document))


def model_accepts(document: Mapping[str, Any]) -> bool:
    try:
        Blueprint.model_validate(document)
        return True
    except ValidationError:
        return False
