from enum import StrEnum

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"


class ErrorCode(StrEnum):
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"
