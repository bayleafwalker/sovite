from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.started_state import StartedState

T = TypeVar("T", bound="Started")


@_attrs_define
class Started:
    """
    Attributes:
        state (StartedState):
        job_id (str):
        generation (str):
        fence (str):
        lease_until (float):
        deadline (float):
    """

    state: StartedState
    job_id: str
    generation: str
    fence: str
    lease_until: float
    deadline: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        job_id = self.job_id

        generation = self.generation

        fence = self.fence

        lease_until = self.lease_until

        deadline = self.deadline

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "job_id": job_id,
                "generation": generation,
                "fence": fence,
                "lease_until": lease_until,
                "deadline": deadline,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        state = StartedState(d.pop("state"))

        job_id = d.pop("job_id")

        generation = d.pop("generation")

        fence = d.pop("fence")

        lease_until = d.pop("lease_until")

        deadline = d.pop("deadline")

        started = cls(
            state=state,
            job_id=job_id,
            generation=generation,
            fence=fence,
            lease_until=lease_until,
            deadline=deadline,
        )

        started.additional_properties = d
        return started

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
