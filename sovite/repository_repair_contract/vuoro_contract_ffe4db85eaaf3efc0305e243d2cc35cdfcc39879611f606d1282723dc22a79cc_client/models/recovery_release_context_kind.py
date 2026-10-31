from enum import StrEnum


class RecoveryReleaseContextKind(StrEnum):
    REPOSITORY_RECOVERY_ARTIFACT = "repository-recovery-artifact"

    def __str__(self) -> str:
        return str(self.value)
