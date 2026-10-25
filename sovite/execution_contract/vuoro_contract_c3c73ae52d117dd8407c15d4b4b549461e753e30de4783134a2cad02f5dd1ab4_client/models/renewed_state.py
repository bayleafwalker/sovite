from enum import StrEnum


class RenewedState(StrEnum):
    RENEWED = "renewed"

    def __str__(self) -> str:
        return str(self.value)
