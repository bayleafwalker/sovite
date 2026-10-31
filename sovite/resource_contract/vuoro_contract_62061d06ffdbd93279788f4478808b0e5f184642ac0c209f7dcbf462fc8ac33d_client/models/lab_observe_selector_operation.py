from enum import StrEnum


class LabObserveSelectorOperation(StrEnum):
    KVM_LAB_OBSERVE = "kvm-lab.observe"

    def __str__(self) -> str:
        return str(self.value)
