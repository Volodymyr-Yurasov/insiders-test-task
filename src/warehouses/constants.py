from enum import StrEnum


class ErrorCode(StrEnum):
    WAREHOUSE_NOT_FOUND = "warehouse_not_found"
    LOCATION_CODE_TAKEN = "location_code_taken"
    WAREHOUSE_NOT_EMPTY = "warehouse_not_empty"
