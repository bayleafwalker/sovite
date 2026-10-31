from enum import StrEnum


class SubmittedState(StrEnum):
    OBSERVED = "observed"
    PENDING = "pending"

    def __str__(self) -> str:
        return str(self.value)
