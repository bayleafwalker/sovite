from enum import StrEnum


class LabKnownFieldsEffectOutcome(StrEnum):
    KNOWN = "known"

    def __str__(self) -> str:
        return str(self.value)
