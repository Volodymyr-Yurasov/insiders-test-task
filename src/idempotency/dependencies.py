from typing import Annotated

from fastapi import Header

from src.idempotency.constants import HEADER

# Optional; 1-255 visible ASCII characters (anything else is a 422).
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=HEADER,
        min_length=1,
        max_length=255,
        pattern=r"^[\x21-\x7E]+$",
        description="Repeat-safe retries: the same key + same request replays the first result "
        "for 24 h instead of writing again. Scoped per user.",
    ),
]
