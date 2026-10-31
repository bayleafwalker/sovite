from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="RecoveryContextRequest")


@_attrs_define
class RecoveryContextRequest:
    """
    Attributes:
        workspace_id (str):
        attempt_id (str):
        source_digest (str):
        contract_id (str):
        destination_id (str):
    """

    workspace_id: str
    attempt_id: str
    source_digest: str
    contract_id: str
    destination_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        attempt_id = self.attempt_id

        source_digest = self.source_digest

        contract_id = self.contract_id

        destination_id = self.destination_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
                "attempt_id": attempt_id,
                "source_digest": source_digest,
                "contract_id": contract_id,
                "destination_id": destination_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        attempt_id = d.pop("attempt_id")

        source_digest = d.pop("source_digest")

        contract_id = d.pop("contract_id")

        destination_id = d.pop("destination_id")

        recovery_context_request = cls(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            source_digest=source_digest,
            contract_id=contract_id,
            destination_id=destination_id,
        )

        recovery_context_request.additional_properties = d
        return recovery_context_request

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
