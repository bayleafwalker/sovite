from enum import StrEnum


class LabHostObservationSchemaVersion(StrEnum):
    CLOUD_KVM_LAB_OBSERVATION_V1 = "cloud.kvm-lab-observation.v1"

    def __str__(self) -> str:
        return str(self.value)
