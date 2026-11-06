from enum import StrEnum


class AgentHttpRecipeProfile(StrEnum):
    AGENT_HTTP_V1 = "agent-http-v1"

    def __str__(self) -> str:
        return str(self.value)
