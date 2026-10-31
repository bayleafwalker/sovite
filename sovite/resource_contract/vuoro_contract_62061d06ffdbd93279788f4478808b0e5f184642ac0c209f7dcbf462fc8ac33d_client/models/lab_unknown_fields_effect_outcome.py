from enum import StrEnum


class LabUnknownFieldsEffectOutcome(StrEnum):
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
