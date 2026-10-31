from enum import StrEnum


class LabCreatedKnownEffectOutcome(StrEnum):
    KNOWN = "known"

    def __str__(self) -> str:
        return str(self.value)
