from enum import StrEnum


class ExecutionStatusPhase(StrEnum):
    FINISHED = "finished"
    RESERVED = "reserved"
    STARTED = "started"

    def __str__(self) -> str:
        return str(self.value)
