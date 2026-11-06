from enum import StrEnum


class OperationFoundState(StrEnum):
    FOUND = "found"

    def __str__(self) -> str:
        return str(self.value)
