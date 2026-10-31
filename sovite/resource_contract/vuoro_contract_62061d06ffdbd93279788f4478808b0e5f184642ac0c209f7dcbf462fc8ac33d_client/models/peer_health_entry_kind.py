from enum import StrEnum


class PeerHealthEntryKind(StrEnum):
    MODEL_PEER_HEALTH = "model-peer-health"

    def __str__(self) -> str:
        return str(self.value)
