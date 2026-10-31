from enum import StrEnum


class PeerHealthObservationSchemaVersion(StrEnum):
    SOVITE_RESOURCE_PEER_HEALTHV1 = "sovite-resource-peer-health/v1"

    def __str__(self) -> str:
        return str(self.value)
