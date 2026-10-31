from enum import StrEnum


class PeerHealthObservationHealthScope(StrEnum):
    PEER_HTTP_DAEMON = "peer-http-daemon"

    def __str__(self) -> str:
        return str(self.value)
