from datetime import datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StringConstraints

from src.auth.constants import Role
from src.auth.utils import normalize_username
from src.schemas import CustomModel, PatchModel


def _normalize(value: object) -> object:
    return normalize_username(value) if isinstance(value, str) else value


# Normalized first, then validated, so " Keeper " becomes "keeper" and passes.
Username = Annotated[
    str,
    BeforeValidator(_normalize),
    StringConstraints(pattern=r"^[a-z0-9_.-]{3,64}$"),
]
Password = Annotated[str, Field(min_length=8, max_length=128)]


class TokenPair(CustomModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"


class RefreshRequest(CustomModel):
    refresh_token: Annotated[str, Field(min_length=1, max_length=256)]


class UserCreate(CustomModel):
    username: Username
    password: Password
    role: Role


class UserUpdate(PatchModel):
    username: Username | None = None
    role: Role | None = None
    password: Password | None = None
    is_active: bool | None = None


class UserRead(CustomModel):
    id: int
    username: str
    role: Role
    is_active: bool
    created_at: datetime
    updated_at: datetime
