from enum import StrEnum


class LabStoppedUnknownEffectOutcome(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
