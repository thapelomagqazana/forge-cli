"""Shared domain models."""

from pydantic import BaseModel


class BlueprintMetadata(BaseModel):
    id: str
    name: str
    version: str
    description: str
    category: str = "backend"
