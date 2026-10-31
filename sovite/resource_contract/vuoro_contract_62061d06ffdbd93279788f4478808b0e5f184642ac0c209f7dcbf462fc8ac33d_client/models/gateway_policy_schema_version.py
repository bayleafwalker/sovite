from enum import StrEnum


class GatewayPolicySchemaVersion(StrEnum):
    SOVITE_RESOURCE_POLICYV2 = "sovite-resource-policy/v2"

    def __str__(self) -> str:
        return str(self.value)
