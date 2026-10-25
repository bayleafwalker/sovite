from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.agent_http_recipe_profile import AgentHttpRecipeProfile
from ..types import UNSET, Unset

T = TypeVar("T", bound="AgentHttpRecipe")


@_attrs_define
class AgentHttpRecipe:
    """
    Attributes:
        profile (AgentHttpRecipeProfile):
        agent_id (str):
        endpoint (str):
        idempotent (bool):
        timeout_seconds (int):
        cancel_endpoint (str | Unset):
        credential_env (str | Unset):
    """

    profile: AgentHttpRecipeProfile
    agent_id: str
    endpoint: str
    idempotent: bool
    timeout_seconds: int
    cancel_endpoint: str | Unset = UNSET
    credential_env: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile = self.profile.value

        agent_id = self.agent_id

        endpoint = self.endpoint

        idempotent = self.idempotent

        timeout_seconds = self.timeout_seconds

        cancel_endpoint = self.cancel_endpoint

        credential_env = self.credential_env

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile": profile,
                "agent_id": agent_id,
                "endpoint": endpoint,
                "idempotent": idempotent,
                "timeout_seconds": timeout_seconds,
            }
        )
        if cancel_endpoint is not UNSET:
            field_dict["cancel_endpoint"] = cancel_endpoint
        if credential_env is not UNSET:
            field_dict["credential_env"] = credential_env

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        profile = AgentHttpRecipeProfile(d.pop("profile"))

        agent_id = d.pop("agent_id")

        endpoint = d.pop("endpoint")

        idempotent = d.pop("idempotent")

        timeout_seconds = d.pop("timeout_seconds")

        cancel_endpoint = d.pop("cancel_endpoint", UNSET)

        credential_env = d.pop("credential_env", UNSET)

        agent_http_recipe = cls(
            profile=profile,
            agent_id=agent_id,
            endpoint=endpoint,
            idempotent=idempotent,
            timeout_seconds=timeout_seconds,
            cancel_endpoint=cancel_endpoint,
            credential_env=credential_env,
        )

        agent_http_recipe.additional_properties = d
        return agent_http_recipe

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
