from enum import StrEnum


class PullIdleState(StrEnum):
    IDLE = "idle"

    def __str__(self) -> str:
        return str(self.value)
