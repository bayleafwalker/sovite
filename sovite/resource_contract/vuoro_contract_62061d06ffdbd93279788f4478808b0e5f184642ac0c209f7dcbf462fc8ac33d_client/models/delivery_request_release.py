from enum import StrEnum


class DeliveryRequestRelease(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"

    def __str__(self) -> str:
        return str(self.value)
