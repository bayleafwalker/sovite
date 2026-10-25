from enum import StrEnum


class WorkerOutcome(StrEnum):
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYABLE_FAILURE = "retryable_failure"

    def __str__(self) -> str:
        return str(self.value)
