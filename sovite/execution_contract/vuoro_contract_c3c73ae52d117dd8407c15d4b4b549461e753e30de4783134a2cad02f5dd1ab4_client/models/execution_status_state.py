from enum import StrEnum


class ExecutionStatusState(StrEnum):
    CURRENT = "current"
    EXPIRED = "expired"
    FINISHED = "finished"
    SUPERSEDED = "superseded"

    def __str__(self) -> str:
        return str(self.value)
