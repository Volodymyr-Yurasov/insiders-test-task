from sqlalchemy import Identity, String, true
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base, TimestampMixin


class Warehouse(TimestampMixin, Base):
    __tablename__ = "warehouse"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    location_code: Mapped[str] = mapped_column(String(32), unique=True)
    address: Mapped[str] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
