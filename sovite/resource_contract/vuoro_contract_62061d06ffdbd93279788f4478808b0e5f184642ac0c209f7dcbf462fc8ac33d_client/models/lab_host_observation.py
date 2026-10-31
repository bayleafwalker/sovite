from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_host_observation_schema_version import LabHostObservationSchemaVersion
from ..models.lab_host_observation_state import LabHostObservationState
from ..models.lab_host_observation_template_id import LabHostObservationTemplateId

T = TypeVar("T", bound="LabHostObservation")


@_attrs_define
class LabHostObservation:
    """
    Attributes:
        schema_version (LabHostObservationSchemaVersion):
        operation_id (str):
        create_operation_id (str):
        template_id (LabHostObservationTemplateId):
        template_sha256 (str):
        deadline (float):
        state (LabHostObservationState):
        host_boot_id (str):
        created_boot_id (str):
        unit (str):
        invocation_id (str):
    """

    schema_version: LabHostObservationSchemaVersion
    operation_id: str
    create_operation_id: str
    template_id: LabHostObservationTemplateId
    template_sha256: str
    deadline: float
    state: LabHostObservationState
    host_boot_id: str
    created_boot_id: str
    unit: str
    invocation_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        operation_id = self.operation_id

        create_operation_id = self.create_operation_id

        template_id = self.template_id.value

        template_sha256 = self.template_sha256

        deadline = self.deadline

        state = self.state.value

        host_boot_id = self.host_boot_id

        created_boot_id = self.created_boot_id

        unit = self.unit

        invocation_id = self.invocation_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "operation_id": operation_id,
                "create_operation_id": create_operation_id,
                "template_id": template_id,
                "template_sha256": template_sha256,
                "deadline": deadline,
                "state": state,
                "host_boot_id": host_boot_id,
                "created_boot_id": created_boot_id,
                "unit": unit,
                "invocation_id": invocation_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        schema_version = LabHostObservationSchemaVersion(d.pop("schema_version"))

        operation_id = d.pop("operation_id")

        create_operation_id = d.pop("create_operation_id")

        template_id = LabHostObservationTemplateId(d.pop("template_id"))

        template_sha256 = d.pop("template_sha256")

        deadline = d.pop("deadline")

        state = LabHostObservationState(d.pop("state"))

        host_boot_id = d.pop("host_boot_id")

        created_boot_id = d.pop("created_boot_id")

        unit = d.pop("unit")

        invocation_id = d.pop("invocation_id")

        lab_host_observation = cls(
            schema_version=schema_version,
            operation_id=operation_id,
            create_operation_id=create_operation_id,
            template_id=template_id,
            template_sha256=template_sha256,
            deadline=deadline,
            state=state,
            host_boot_id=host_boot_id,
            created_boot_id=created_boot_id,
            unit=unit,
            invocation_id=invocation_id,
        )

        lab_host_observation.additional_properties = d
        return lab_host_observation

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
