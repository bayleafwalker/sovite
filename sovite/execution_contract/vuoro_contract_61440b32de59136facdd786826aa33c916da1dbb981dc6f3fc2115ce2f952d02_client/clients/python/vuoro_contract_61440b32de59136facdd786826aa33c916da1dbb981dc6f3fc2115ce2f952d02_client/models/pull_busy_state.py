from enum import StrEnum


class PullBusyState(StrEnum):
    BUSY = "busy"

    def __str__(self) -> str:
        return str(self.value)
