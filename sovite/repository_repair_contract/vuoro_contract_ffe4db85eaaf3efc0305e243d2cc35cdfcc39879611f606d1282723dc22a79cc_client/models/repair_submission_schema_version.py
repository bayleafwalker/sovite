from enum import StrEnum


class RepairSubmissionSchemaVersion(StrEnum):
    SOVITE_REPOSITORY_REPAIRV1 = "sovite-repository-repair/v1"

    def __str__(self) -> str:
        return str(self.value)
