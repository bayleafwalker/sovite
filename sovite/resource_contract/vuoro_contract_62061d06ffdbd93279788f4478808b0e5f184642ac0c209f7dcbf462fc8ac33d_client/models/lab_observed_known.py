from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_observed_known_effect_outcome import LabObservedKnownEffectOutcome
from ..models.lab_observed_known_operation import LabObservedKnownOperation
from ..models.lab_observed_known_schema_version import LabObservedKnownSchemaVersion
from ..models.lab_observed_known_state import LabObservedKnownState

T = TypeVar("T", bound="LabObservedKnown")


@_attrs_define
class LabObservedKnown:
    """
    Attributes:
        workspace_id (str):
        resource_domain_id (str):
        target_id (str):
        lab_id (str):
        deadline (float):
        state (LabObservedKnownState):
        effect_outcome (LabObservedKnownEffectOutcome):
        schema_version (LabObservedKnownSchemaVersion):
        operation (LabObservedKnownOperation):
    """

    workspace_id: str
    resource_domain_id: str
    target_id: str
    lab_id: str
    deadline: float
    state: LabObservedKnownState
    effect_outcome: LabObservedKnownEffectOutcome
    schema_version: LabObservedKnownSchemaVersion
    operation: LabObservedKnownOperation
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        resource_domain_id = self.resource_domain_id

        target_id = self.target_id

        lab_id = self.lab_id

        deadline = self.deadline

        state = self.state.value

        effect_outcome = self.effect_outcome.value

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
                "deadline": deadline,
                "state": state,
                "effect_outcome": effect_outcome,
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

        deadline = d.pop("deadline")

        state = LabObservedKnownState(d.pop("state"))

        effect_outcome = LabObservedKnownEffectOutcome(d.pop("effect_outcome"))

        schema_version = LabObservedKnownSchemaVersion(d.pop("schema_version"))

        operation = LabObservedKnownOperation(d.pop("operation"))

        lab_observed_known = cls(
            workspace_id=workspace_id,
            resource_domain_id=resource_domain_id,
            target_id=target_id,
            lab_id=lab_id,
            deadline=deadline,
            state=state,
            effect_outcome=effect_outcome,
            schema_version=schema_version,
            operation=operation,
        )

        lab_observed_known.additional_properties = d
        return lab_observed_known

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
