from enum import StrEnum


class PullReservedState(StrEnum):
    RESERVED = "reserved"

    def __str__(self) -> str:
        return str(self.value)
