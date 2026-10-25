from enum import StrEnum


class OperationFoundResultState(StrEnum):
    ALREADY_STARTED = "already_started"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    HANDED_BACK = "handed_back"
    IDLE = "idle"
    RENEWED = "renewed"
    RESERVED = "reserved"
    RETRY_WAIT = "retry_wait"
    STARTED = "started"

    def __str__(self) -> str:
        return str(self.value)
