from enum import StrEnum


class OutputGrantInstalledSchemaVersion(StrEnum):
    SOVITE_RESOURCE_LAB_CREATEDV1 = "sovite-resource-lab-created/v1"
    SOVITE_RESOURCE_LAB_OBSERVEDV1 = "sovite-resource-lab-observed/v1"
    SOVITE_RESOURCE_LAB_STOPPEDV1 = "sovite-resource-lab-stopped/v1"
    SOVITE_RESOURCE_PEER_HEALTHV1 = "sovite-resource-peer-health/v1"

    def __str__(self) -> str:
        return str(self.value)
