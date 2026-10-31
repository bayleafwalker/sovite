from enum import StrEnum


class LabCreatedKnownState(StrEnum):
    ABSENT = "absent"
    RUNNING = "running"
    STOPPED = "stopped"

    def __str__(self) -> str:
        return str(self.value)
