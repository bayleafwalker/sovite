from enum import StrEnum


class LabObservedKnownEffectOutcome(StrEnum):
    KNOWN = "known"

    def __str__(self) -> str:
        return str(self.value)
