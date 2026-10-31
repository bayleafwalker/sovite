from enum import StrEnum


class LabUnknownFieldsState(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
