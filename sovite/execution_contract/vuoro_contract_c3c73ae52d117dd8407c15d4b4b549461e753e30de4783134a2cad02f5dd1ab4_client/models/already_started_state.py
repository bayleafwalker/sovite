from enum import StrEnum


class AlreadyStartedState(StrEnum):
    ALREADY_STARTED = "already_started"

    def __str__(self) -> str:
        return str(self.value)
