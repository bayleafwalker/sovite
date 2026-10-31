from enum import StrEnum


class PeerHealthMessageMessage(StrEnum):
    OK = "ok"

    def __str__(self) -> str:
        return str(self.value)
