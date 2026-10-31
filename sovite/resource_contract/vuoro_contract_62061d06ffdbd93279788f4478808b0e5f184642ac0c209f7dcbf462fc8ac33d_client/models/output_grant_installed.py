from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.output_grant_installed_schema_version import (
    OutputGrantInstalledSchemaVersion,
)

T = TypeVar("T", bound="OutputGrantInstalled")


@_attrs_define
class OutputGrantInstalled:
    """
    Attributes:
        grant_id (str):
        schema_version (OutputGrantInstalledSchemaVersion):
        destination (str):
        policy_revision (str):
    """

    grant_id: str
    schema_version: OutputGrantInstalledSchemaVersion
    destination: str
    policy_revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        grant_id = self.grant_id

        schema_version = self.schema_version.value

        destination = self.destination

        policy_revision = self.policy_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "grant_id": grant_id,
                "schema_version": schema_version,
                "destination": destination,
                "policy_revision": policy_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        grant_id = d.pop("grant_id")

        schema_version = OutputGrantInstalledSchemaVersion(d.pop("schema_version"))

        destination = d.pop("destination")

        policy_revision = d.pop("policy_revision")

        output_grant_installed = cls(
            grant_id=grant_id,
            schema_version=schema_version,
            destination=destination,
            policy_revision=policy_revision,
        )

        output_grant_installed.additional_properties = d
        return output_grant_installed

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
