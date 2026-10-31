from enum import StrEnum


class LabCreatedUnknownState(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
