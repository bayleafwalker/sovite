from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_stopped_effect_outcome import LabStoppedEffectOutcome
from ..models.lab_stopped_operation import LabStoppedOperation
from ..models.lab_stopped_schema_version import LabStoppedSchemaVersion
from ..models.lab_stopped_state import LabStoppedState

T = TypeVar("T", bound="LabStopped")


@_attrs_define
class LabStopped:
    """
    Attributes:
        workspace_id (str):
        resource_domain_id (str):
        target_id (str):
        lab_id (str):
        state (LabStoppedState):
        effect_outcome (LabStoppedEffectOutcome):
        deadline (float):
        schema_version (LabStoppedSchemaVersion):
        operation (LabStoppedOperation):
    """

    workspace_id: str
    resource_domain_id: str
    target_id: str
    lab_id: str
    state: LabStoppedState
    effect_outcome: LabStoppedEffectOutcome
    deadline: float
    schema_version: LabStoppedSchemaVersion
    operation: LabStoppedOperation
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        resource_domain_id = self.resource_domain_id

        target_id = self.target_id

        lab_id = self.lab_id

        state = self.state.value

        effect_outcome = self.effect_outcome.value

        deadline = self.deadline

        schema_version = self.schema_version.value

        operation = self.operation.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
                "resource_domain_id": resource_domain_id,
                "target_id": target_id,
                "lab_id": lab_id,
                "state": state,
                "effect_outcome": effect_outcome,
                "deadline": deadline,
                "schema_version": schema_version,
                "operation": operation,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        resource_domain_id = d.pop("resource_domain_id")

        target_id = d.pop("target_id")

        lab_id = d.pop("lab_id")

        state = LabStoppedState(d.pop("state"))

        effect_outcome = LabStoppedEffectOutcome(d.pop("effect_outcome"))

        deadline = d.pop("deadline")

        schema_version = LabStoppedSchemaVersion(d.pop("schema_version"))

        operation = LabStoppedOperation(d.pop("operation"))

        lab_stopped = cls(
            workspace_id=workspace_id,
            resource_domain_id=resource_domain_id,
            target_id=target_id,
            lab_id=lab_id,
            state=state,
            effect_outcome=effect_outcome,
            deadline=deadline,
            schema_version=schema_version,
            operation=operation,
        )

        lab_stopped.additional_properties = d
        return lab_stopped

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
