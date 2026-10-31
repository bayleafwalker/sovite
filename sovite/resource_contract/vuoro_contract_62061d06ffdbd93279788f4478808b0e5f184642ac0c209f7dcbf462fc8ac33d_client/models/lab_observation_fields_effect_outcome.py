from enum import StrEnum


class LabObservationFieldsEffectOutcome(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
