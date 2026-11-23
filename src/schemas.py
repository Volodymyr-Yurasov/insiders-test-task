from decimal import Decimal
from typing import Annotated, Any, Self

from fastapi import Path, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CENTS = Decimal("0.01")

# Primary keys are INTEGER (int32). Bounding every incoming id makes out-of-range values a
# 422 instead of a driver error (500) when asyncpg refuses to encode them.
INT32_MAX = 2_147_483_647
DbId = Annotated[int, Field(ge=1, le=INT32_MAX)]  # body fields / query models
IdPath = Annotated[int, Path(ge=1, le=INT32_MAX)]
IdQuery = Annotated[int | None, Query(ge=1, le=INT32_MAX)]

# Human-readable names: trimmed, non-empty.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def strip_upper(value: object) -> object:
    """BeforeValidator for codes (SKU, location code): ' ab-1 ' -> 'AB-1'."""
    return value.strip().upper() if isinstance(value, str) else value


class CustomModel(BaseModel):
    """Global base for all API schemas."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PatchModel(CustomModel):
    """Base for partial updates: omitted or null fields mean "unchanged"; an empty body is 422.

    Every editable domain field is non-nullable, so null never needs to mean "clear".
    """

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.changes():
            raise ValueError("Provide at least one field to update")
        return self

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
