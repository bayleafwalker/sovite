from enum import StrEnum


class GatewayRequestAudience(StrEnum):
    RESOURCE_GATEWAY = "resource-gateway"

    def __str__(self) -> str:
        return str(self.value)
