from enum import StrEnum


class HandedBackDisposition(StrEnum):
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    RETRY_WAIT = "retry_wait"

    def __str__(self) -> str:
        return str(self.value)
