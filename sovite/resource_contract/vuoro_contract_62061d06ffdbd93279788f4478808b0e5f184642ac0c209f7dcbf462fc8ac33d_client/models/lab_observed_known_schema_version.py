from enum import StrEnum


class LabObservedKnownSchemaVersion(StrEnum):
    SOVITE_RESOURCE_LAB_OBSERVEDV1 = "sovite-resource-lab-observed/v1"

    def __str__(self) -> str:
        return str(self.value)
