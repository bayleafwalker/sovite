from enum import StrEnum


class LabObservedState(StrEnum):
    ABSENT = "absent"
    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
