from enum import StrEnum


class LabObservedKnownState(StrEnum):
    ABSENT = "absent"
    RUNNING = "running"
    STOPPED = "stopped"

    def __str__(self) -> str:
        return str(self.value)
