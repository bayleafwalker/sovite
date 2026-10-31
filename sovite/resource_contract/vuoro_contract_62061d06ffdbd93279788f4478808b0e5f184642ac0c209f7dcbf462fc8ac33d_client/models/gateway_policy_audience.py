from enum import StrEnum


class GatewayPolicyAudience(StrEnum):
    RESOURCE_GATEWAY = "resource-gateway"

    def __str__(self) -> str:
        return str(self.value)
