from enum import StrEnum


class LabCreatedKnownSchemaVersion(StrEnum):
    SOVITE_RESOURCE_LAB_CREATEDV1 = "sovite-resource-lab-created/v1"

    def __str__(self) -> str:
        return str(self.value)
