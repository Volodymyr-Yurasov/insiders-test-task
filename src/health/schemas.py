from typing import Literal

from src.schemas import CustomModel

ComponentStatus = Literal["ok", "error"]


class HealthResponse(CustomModel):
    status: ComponentStatus
    db: ComponentStatus
    redis: ComponentStatus
