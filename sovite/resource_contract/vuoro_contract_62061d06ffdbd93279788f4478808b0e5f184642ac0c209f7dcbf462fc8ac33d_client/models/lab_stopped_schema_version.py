from enum import StrEnum


class LabStoppedSchemaVersion(StrEnum):
    SOVITE_RESOURCE_LAB_STOPPEDV1 = "sovite-resource-lab-stopped/v1"

    def __str__(self) -> str:
        return str(self.value)
