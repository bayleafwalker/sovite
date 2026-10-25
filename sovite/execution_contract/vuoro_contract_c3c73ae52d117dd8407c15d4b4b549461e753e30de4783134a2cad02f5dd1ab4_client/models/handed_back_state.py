from enum import StrEnum


class HandedBackState(StrEnum):
    HANDED_BACK = "handed_back"

    def __str__(self) -> str:
        return str(self.value)
