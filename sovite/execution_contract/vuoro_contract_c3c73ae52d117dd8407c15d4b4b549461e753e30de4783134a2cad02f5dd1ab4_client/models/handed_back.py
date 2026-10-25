from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.handed_back_disposition import HandedBackDisposition
from ..models.handed_back_state import HandedBackState

T = TypeVar("T", bound="HandedBack")


@_attrs_define
class HandedBack:
    """
    Attributes:
        state (HandedBackState):
        job_id (str):
        generation (str):
        fence (str):
        disposition (HandedBackDisposition):
    """

    state: HandedBackState
    job_id: str
    generation: str
    fence: str
    disposition: HandedBackDisposition
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        job_id = self.job_id

        generation = self.generation

        fence = self.fence

        disposition = self.disposition.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "job_id": job_id,
                "generation": generation,
                "fence": fence,
                "disposition": disposition,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        state = HandedBackState(d.pop("state"))

        job_id = d.pop("job_id")

        generation = d.pop("generation")

        fence = d.pop("fence")

        disposition = HandedBackDisposition(d.pop("disposition"))

        handed_back = cls(
            state=state,
            job_id=job_id,
            generation=generation,
            fence=fence,
            disposition=disposition,
        )

        handed_back.additional_properties = d
        return handed_back

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
