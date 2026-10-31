from enum import StrEnum


class TargetCatalogSchemaVersion(StrEnum):
    SOVITE_RESOURCE_CATALOGV2 = "sovite-resource-catalog/v2"

    def __str__(self) -> str:
        return str(self.value)
