from src.exceptions import Conflict, NotFound
from src.warehouses.constants import ErrorCode


class WarehouseNotFound(NotFound):
    CODE = ErrorCode.WAREHOUSE_NOT_FOUND
    DETAIL = "Warehouse not found"


class LocationCodeTaken(Conflict):
    CODE = ErrorCode.LOCATION_CODE_TAKEN
    DETAIL = "Location code is already used by another warehouse"
