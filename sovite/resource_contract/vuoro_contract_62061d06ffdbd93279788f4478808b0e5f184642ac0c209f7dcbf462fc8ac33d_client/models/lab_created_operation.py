from enum import StrEnum


class LabCreatedOperation(StrEnum):
    KVM_LAB_CREATE = "kvm-lab.create"

    def __str__(self) -> str:
        return str(self.value)
