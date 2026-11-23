from typing import Annotated

from fastapi import Depends

from src.notifications.service import LoggingNotifier, Notifier

_default_notifier = LoggingNotifier()


async def get_notifier() -> Notifier:
    # Swap with app.dependency_overrides[get_notifier] (tests, or a real email gateway).
    return _default_notifier


NotifierDep = Annotated[Notifier, Depends(get_notifier)]
