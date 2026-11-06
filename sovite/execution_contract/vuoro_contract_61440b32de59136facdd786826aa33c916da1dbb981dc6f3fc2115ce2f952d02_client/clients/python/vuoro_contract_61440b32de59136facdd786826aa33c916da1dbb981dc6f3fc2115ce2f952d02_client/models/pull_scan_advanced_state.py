from enum import StrEnum


class PullScanAdvancedState(StrEnum):
    SCAN_ADVANCED = "scan_advanced"

    def __str__(self) -> str:
        return str(self.value)
