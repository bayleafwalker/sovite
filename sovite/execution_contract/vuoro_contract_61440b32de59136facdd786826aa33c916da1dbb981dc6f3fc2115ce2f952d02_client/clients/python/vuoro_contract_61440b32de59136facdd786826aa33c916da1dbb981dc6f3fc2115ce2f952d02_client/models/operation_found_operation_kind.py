from enum import StrEnum


class OperationFoundOperationKind(StrEnum):
    COMPLETE = "complete"
    HANDBACK = "handback"
    PULL = "pull"
    RENEW = "renew"
    START = "start"

    def __str__(self) -> str:
        return str(self.value)
