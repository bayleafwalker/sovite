from enum import StrEnum


class C2ProfileSourceClass(StrEnum):
    C2 = "C2"

    def __str__(self) -> str:
        return str(self.value)
