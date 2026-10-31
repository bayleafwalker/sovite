from enum import StrEnum


class C2ProfileOutputClass(StrEnum):
    C2 = "C2"

    def __str__(self) -> str:
        return str(self.value)
