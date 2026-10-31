from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="CurrentRequest")


@_attrs_define
class CurrentRequest:
    """
    Attributes:
        request_id (str):
        fence_fingerprint (str):
        policy_revision (str):
        destination (str):
        actor (str):
    """

    request_id: str
    fence_fingerprint: str
    policy_revision: str
    destination: str
    actor: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id

        fence_fingerprint = self.fence_fingerprint

        policy_revision = self.policy_revision

        destination = self.destination

        actor = self.actor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "fence_fingerprint": fence_fingerprint,
                "policy_revision": policy_revision,
                "destination": destination,
                "actor": actor,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        request_id = d.pop("request_id")

        fence_fingerprint = d.pop("fence_fingerprint")

        policy_revision = d.pop("policy_revision")

        destination = d.pop("destination")

        actor = d.pop("actor")

        current_request = cls(
            request_id=request_id,
            fence_fingerprint=fence_fingerprint,
            policy_revision=policy_revision,
            destination=destination,
            actor=actor,
        )

        current_request.additional_properties = d
        return current_request

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
