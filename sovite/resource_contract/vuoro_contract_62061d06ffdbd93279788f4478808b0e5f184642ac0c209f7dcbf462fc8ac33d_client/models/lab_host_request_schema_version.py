from enum import StrEnum


class LabHostRequestSchemaVersion(StrEnum):
    CLOUD_KVM_LAB_CONTROL_V1 = "cloud.kvm-lab-control.v1"

    def __str__(self) -> str:
        return str(self.value)
