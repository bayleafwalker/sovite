from enum import StrEnum


class InstalledCatalogSchemaVersion(StrEnum):
    SOVITE_RESOURCE_INSTALLED_CATALOGV2 = "sovite-resource-installed-catalog/v2"

    def __str__(self) -> str:
        return str(self.value)
