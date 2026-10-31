from enum import StrEnum


class LabObservedEffectOutcome(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
