from enum import StrEnum


class LabObservedUnknownEffectOutcome(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
