from enum import StrEnum


class LabKnownFieldsState(StrEnum):
    ABSENT = "absent"
    RUNNING = "running"
    STOPPED = "stopped"

    def __str__(self) -> str:
        return str(self.value)
