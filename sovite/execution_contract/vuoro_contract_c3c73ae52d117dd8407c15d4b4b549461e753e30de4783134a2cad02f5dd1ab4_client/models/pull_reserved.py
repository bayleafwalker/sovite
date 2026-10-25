from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.pull_reserved_state import PullReservedState

if TYPE_CHECKING:
    from ..models.reserved_job import ReservedJob


T = TypeVar("T", bound="PullReserved")


@_attrs_define
class PullReserved:
    """
    Attributes:
        state (PullReservedState):
        job (ReservedJob):
    """

    state: PullReservedState
    job: ReservedJob
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        job = self.job.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "job": job,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.reserved_job import ReservedJob

        d = dict(src_dict)
        state = PullReservedState(d.pop("state"))

        job = ReservedJob.from_dict(d.pop("job"))

        pull_reserved = cls(
            state=state,
            job=job,
        )

        pull_reserved.additional_properties = d
        return pull_reserved

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
