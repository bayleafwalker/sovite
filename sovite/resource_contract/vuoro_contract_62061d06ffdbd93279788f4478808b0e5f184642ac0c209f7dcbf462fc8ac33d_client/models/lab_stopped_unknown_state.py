from enum import StrEnum


class LabStoppedUnknownState(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
