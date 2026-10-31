from enum import StrEnum


class LabStopSelectorOperation(StrEnum):
    KVM_LAB_STOP = "kvm-lab.stop"

    def __str__(self) -> str:
        return str(self.value)
