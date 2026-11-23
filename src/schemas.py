from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator


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
