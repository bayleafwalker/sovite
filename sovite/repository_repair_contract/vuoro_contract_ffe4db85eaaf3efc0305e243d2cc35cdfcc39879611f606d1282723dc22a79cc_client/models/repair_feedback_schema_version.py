from enum import StrEnum


class RepairFeedbackSchemaVersion(StrEnum):
    SOVITE_REPOSITORY_REPAIR_FEEDBACKV1 = "sovite-repository-repair-feedback/v1"

    def __str__(self) -> str:
        return str(self.value)
