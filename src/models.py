"""Model registry: importing this module registers every table on `Base.metadata`.

Imported by alembic/env.py (autogenerate) and the test harness (TRUNCATE). Without it, an
unimported model is invisible and autogenerate would propose dropping its table.
"""

import src.auth.models
import src.movements.models
import src.products.models
import src.warehouses.models  # noqa: F401
from src.database import Base

__all__ = ["Base"]
