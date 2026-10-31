from enum import StrEnum


class RecoveryCapturedOutcome(StrEnum):
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNCERTAIN = "uncertain"

    def __str__(self) -> str:
        return str(self.value)
