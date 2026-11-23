"""Limit/offset pagination with the total from the same query.

`COUNT(*) OVER()` is computed over the full filtered set before LIMIT/OFFSET apply, so the
page and its total come from one snapshot: a separate count query could disagree with the
page under concurrent writes.
"""

from typing import Annotated, Any, Generic, TypeVar

from fastapi import Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.schemas import CustomModel

T = TypeVar("T")


class PageParams(CustomModel):
    limit: int
    offset: int


async def page_params(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


Pagination = Annotated[PageParams, Depends(page_params)]


class Page(CustomModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


async def paginate(db: AsyncSession, stmt: Select[Any], params: PageParams) -> dict[str, Any]:
    """Run `stmt` (already filtered and ORDERED with a unique tie-breaker) for one page.

    Returns a dict matching `Page[...]`; items are whatever the statement's first column
    yields (ORM entities for `select(Model)`), so the route's response_model validates them.
    """
    windowed = stmt.add_columns(func.count().over().label("_total"))
    rows = (await db.execute(windowed.limit(params.limit).offset(params.offset))).all()
    if rows:
        total = rows[0]._total
    elif params.offset == 0:
        total = 0
    else:
        # Past the end: no row to carry the window count, so count separately (rare path).
        total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    return {
        "items": [row[0] for row in rows],
        "total": total,
        "limit": params.limit,
        "offset": params.offset,
    }
