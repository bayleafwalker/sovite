from enum import StrEnum


class RepairFeedbackResult(StrEnum):
    CHECK_FAILED = "check_failed"
    REVIEW_FAILED = "review_failed"

    def __str__(self) -> str:
        return str(self.value)
