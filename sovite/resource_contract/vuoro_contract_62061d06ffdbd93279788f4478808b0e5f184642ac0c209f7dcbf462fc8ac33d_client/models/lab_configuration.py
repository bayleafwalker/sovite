from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_configuration_kind import LabConfigurationKind
from ..models.lab_configuration_template_id import LabConfigurationTemplateId

T = TypeVar("T", bound="LabConfiguration")


@_attrs_define
class LabConfiguration:
    """
    Attributes:
        kind (LabConfigurationKind):
        target_id (str):
        template_id (LabConfigurationTemplateId):
        template_sha256 (str):
        max_ttl_seconds (int):
    """

    kind: LabConfigurationKind
    target_id: str
    template_id: LabConfigurationTemplateId
    template_sha256: str
    max_ttl_seconds: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        target_id = self.target_id

        template_id = self.template_id.value

        template_sha256 = self.template_sha256

        max_ttl_seconds = self.max_ttl_seconds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "target_id": target_id,
                "template_id": template_id,
                "template_sha256": template_sha256,
                "max_ttl_seconds": max_ttl_seconds,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        kind = LabConfigurationKind(d.pop("kind"))

        target_id = d.pop("target_id")

        template_id = LabConfigurationTemplateId(d.pop("template_id"))

        template_sha256 = d.pop("template_sha256")

        max_ttl_seconds = d.pop("max_ttl_seconds")

        lab_configuration = cls(
            kind=kind,
            target_id=target_id,
            template_id=template_id,
            template_sha256=template_sha256,
            max_ttl_seconds=max_ttl_seconds,
        )

        lab_configuration.additional_properties = d
        return lab_configuration

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
