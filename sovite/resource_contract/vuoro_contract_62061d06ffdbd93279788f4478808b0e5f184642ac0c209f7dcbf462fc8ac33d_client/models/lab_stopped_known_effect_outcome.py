from enum import StrEnum


class LabStoppedKnownEffectOutcome(StrEnum):
    KNOWN = "known"

    def __str__(self) -> str:
        return str(self.value)
