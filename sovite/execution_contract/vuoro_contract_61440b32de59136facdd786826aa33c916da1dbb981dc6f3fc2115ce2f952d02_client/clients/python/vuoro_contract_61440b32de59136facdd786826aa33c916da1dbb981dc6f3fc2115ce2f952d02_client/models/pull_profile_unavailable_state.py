from enum import StrEnum


class PullProfileUnavailableState(StrEnum):
    PROFILE_UNAVAILABLE = "profile_unavailable"

    def __str__(self) -> str:
        return str(self.value)
