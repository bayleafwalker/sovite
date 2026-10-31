from enum import StrEnum


class LabHostObservationTemplateId(StrEnum):
    DISKLESS_KVM_LAB_V1 = "diskless-kvm-lab-v1"

    def __str__(self) -> str:
        return str(self.value)
