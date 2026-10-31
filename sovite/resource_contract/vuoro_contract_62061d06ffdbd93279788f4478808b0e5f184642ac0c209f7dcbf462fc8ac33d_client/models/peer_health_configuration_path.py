from enum import StrEnum


class PeerHealthConfigurationPath(StrEnum):
    VALUE_0 = "/healthz"

    def __str__(self) -> str:
        return str(self.value)
