from fastapi import APIRouter, Depends

from src.auth.dependencies import admin_user
from src.database import DbSession
from src.reports import service as reports_service
from src.reports.schemas import ValuationReport

# The financial report is admin-only (spec 2.6): enforced for the whole router.
router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(admin_user)])


@router.get(
    "/valuation",
    response_model=ValuationReport,
    summary="Stock value per warehouse (stock * current base price) and grand total (admin)",
)
async def get_valuation(db: DbSession) -> ValuationReport:
    return await reports_service.compute_valuation(db)
