from enum import StrEnum


class LabConfigurationKind(StrEnum):
    KVM_LAB = "kvm-lab"

    def __str__(self) -> str:
        return str(self.value)
