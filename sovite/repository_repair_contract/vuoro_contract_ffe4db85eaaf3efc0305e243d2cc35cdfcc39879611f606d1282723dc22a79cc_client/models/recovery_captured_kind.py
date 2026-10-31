from enum import StrEnum


class RecoveryCapturedKind(StrEnum):
    REPOSITORY_RECOVERY_ARTIFACT = "repository-recovery-artifact"

    def __str__(self) -> str:
        return str(self.value)
