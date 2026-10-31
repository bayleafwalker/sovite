from enum import StrEnum


class LabHostRequestVerb(StrEnum):
    CREATE = "create"
    OBSERVE = "observe"
    STOP = "stop"

    def __str__(self) -> str:
        return str(self.value)
