from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_host_request_schema_version import LabHostRequestSchemaVersion
from ..models.lab_host_request_template_id import LabHostRequestTemplateId
from ..models.lab_host_request_verb import LabHostRequestVerb

T = TypeVar("T", bound="LabHostRequest")


@_attrs_define
class LabHostRequest:
    """
    Attributes:
        schema_version (LabHostRequestSchemaVersion):
        verb (LabHostRequestVerb):
        operation_id (str):
        create_operation_id (str):
        template_id (LabHostRequestTemplateId):
        template_sha256 (str):
        deadline (float):
    """

    schema_version: LabHostRequestSchemaVersion
    verb: LabHostRequestVerb
    operation_id: str
    create_operation_id: str
    template_id: LabHostRequestTemplateId
    template_sha256: str
    deadline: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        verb = self.verb.value

        operation_id = self.operation_id

        create_operation_id = self.create_operation_id

        template_id = self.template_id.value

        template_sha256 = self.template_sha256

        deadline = self.deadline

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "verb": verb,
                "operation_id": operation_id,
                "create_operation_id": create_operation_id,
                "template_id": template_id,
                "template_sha256": template_sha256,
                "deadline": deadline,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        schema_version = LabHostRequestSchemaVersion(d.pop("schema_version"))

        verb = LabHostRequestVerb(d.pop("verb"))

        operation_id = d.pop("operation_id")

        create_operation_id = d.pop("create_operation_id")

        template_id = LabHostRequestTemplateId(d.pop("template_id"))

        template_sha256 = d.pop("template_sha256")

        deadline = d.pop("deadline")

        lab_host_request = cls(
            schema_version=schema_version,
            verb=verb,
            operation_id=operation_id,
            create_operation_id=create_operation_id,
            template_id=template_id,
            template_sha256=template_sha256,
            deadline=deadline,
        )

        lab_host_request.additional_properties = d
        return lab_host_request

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
