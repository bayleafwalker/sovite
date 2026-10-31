from enum import StrEnum


class ResourceResultOutputClass(StrEnum):
    C0 = "C0"
    C1 = "C1"
    C2 = "C2"

    def __str__(self) -> str:
        return str(self.value)
