from enum import StrEnum


class CompletionState(StrEnum):
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY_WAIT = "retry_wait"

    def __str__(self) -> str:
        return str(self.value)
