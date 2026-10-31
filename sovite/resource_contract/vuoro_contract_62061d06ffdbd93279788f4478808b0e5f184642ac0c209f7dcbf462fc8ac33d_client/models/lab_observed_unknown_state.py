from enum import StrEnum


class LabObservedUnknownState(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
