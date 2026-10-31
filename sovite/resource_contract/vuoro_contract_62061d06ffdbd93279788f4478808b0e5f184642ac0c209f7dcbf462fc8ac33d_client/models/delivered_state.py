from enum import StrEnum


class DeliveredState(StrEnum):
    CONFLICT = "conflict"
    FENCED = "fenced"
    STORED = "stored"
    WITHHELD = "withheld"

    def __str__(self) -> str:
        return str(self.value)
