from enum import StrEnum


class PeerHealthConfigurationMethod(StrEnum):
    GET = "GET"

    def __str__(self) -> str:
        return str(self.value)
