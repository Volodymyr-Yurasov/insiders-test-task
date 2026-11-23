from pydantic import BaseModel, ConfigDict


class CustomModel(BaseModel):
    """Global base for all API schemas."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
