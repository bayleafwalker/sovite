from enum import StrEnum


class C1ProfileSourceClass(StrEnum):
    C1 = "C1"

    def __str__(self) -> str:
        return str(self.value)
