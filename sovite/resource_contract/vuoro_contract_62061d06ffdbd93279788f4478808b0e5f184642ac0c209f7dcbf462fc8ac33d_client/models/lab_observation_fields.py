from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_observation_fields_effect_outcome import (
    LabObservationFieldsEffectOutcome,
)
from ..models.lab_observation_fields_state import LabObservationFieldsState

T = TypeVar("T", bound="LabObservationFields")


@_attrs_define
class LabObservationFields:
    """
    Attributes:
        workspace_id (str):
        resource_domain_id (str):
        target_id (str):
        lab_id (str):
        state (LabObservationFieldsState):
        effect_outcome (LabObservationFieldsEffectOutcome):
        deadline (float):
    """

    workspace_id: str
    resource_domain_id: str
    target_id: str
    lab_id: str
    state: LabObservationFieldsState
    effect_outcome: LabObservationFieldsEffectOutcome
    deadline: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        resource_domain_id = self.resource_domain_id

        target_id = self.target_id

        lab_id = self.lab_id

        state = self.state.value

        effect_outcome = self.effect_outcome.value

        deadline = self.deadline

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

        state = LabObservationFieldsState(d.pop("state"))

        effect_outcome = LabObservationFieldsEffectOutcome(d.pop("effect_outcome"))

        deadline = d.pop("deadline")

        lab_observation_fields = cls(
            workspace_id=workspace_id,
            resource_domain_id=resource_domain_id,
            target_id=target_id,
            lab_id=lab_id,
            state=state,
            effect_outcome=effect_outcome,
            deadline=deadline,
        )

        lab_observation_fields.additional_properties = d
        return lab_observation_fields

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
