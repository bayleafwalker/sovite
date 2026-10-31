from enum import StrEnum


class LabCreatedUnknownEffectOutcome(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
