from enum import StrEnum


class PeerHealthObservationOperation(StrEnum):
    MODEL_PEER_HEALTH_READ = "model-peer.health-read"

    def __str__(self) -> str:
        return str(self.value)
