from enum import StrEnum


class ExecutionStatusJobStatus(StrEnum):
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    QUEUED = "queued"
    RETRY_WAIT = "retry_wait"
    RUNNING = "running"

    def __str__(self) -> str:
        return str(self.value)
