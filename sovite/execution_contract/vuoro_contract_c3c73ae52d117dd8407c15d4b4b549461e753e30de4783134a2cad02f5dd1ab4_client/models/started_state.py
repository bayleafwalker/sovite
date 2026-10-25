from enum import StrEnum


class StartedState(StrEnum):
    STARTED = "started"

    def __str__(self) -> str:
        return str(self.value)
