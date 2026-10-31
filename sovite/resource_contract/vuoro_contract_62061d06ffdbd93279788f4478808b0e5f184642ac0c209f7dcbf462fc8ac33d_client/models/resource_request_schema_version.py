from enum import StrEnum


class ResourceRequestSchemaVersion(StrEnum):
    SOVITE_RESOURCE_REQUESTV2 = "sovite-resource-request/v2"

    def __str__(self) -> str:
        return str(self.value)
