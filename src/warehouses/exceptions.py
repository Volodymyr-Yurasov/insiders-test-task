from src.exceptions import Conflict, NotFound
from src.warehouses.constants import ErrorCode


class WarehouseNotFound(NotFound):
    CODE = ErrorCode.WAREHOUSE_NOT_FOUND
    DETAIL = "Warehouse not found"


class WarehouseNotEmpty(Conflict):
    CODE = ErrorCode.WAREHOUSE_NOT_EMPTY
    DETAIL = "Warehouse still holds stock; move it out before deactivating"


class LocationCodeTaken(Conflict):
    CODE = ErrorCode.LOCATION_CODE_TAKEN
    DETAIL = "Location code is already used by another warehouse"
