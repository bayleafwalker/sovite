from enum import StrEnum


class C0ProfileSourceClass(StrEnum):
    C0 = "C0"

    def __str__(self) -> str:
        return str(self.value)
