from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="StatusRequest")


@_attrs_define
class StatusRequest:
    """
    Attributes:
        instance_id (str):
        job_id (str):
        generation (str):
        fence (str):
    """

    instance_id: str
    job_id: str
    generation: str
    fence: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        instance_id = self.instance_id

        job_id = self.job_id

        generation = self.generation

        fence = self.fence

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "instance_id": instance_id,
                "job_id": job_id,
                "generation": generation,
                "fence": fence,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        instance_id = d.pop("instance_id")

        job_id = d.pop("job_id")

        generation = d.pop("generation")

        fence = d.pop("fence")

        status_request = cls(
            instance_id=instance_id,
            job_id=job_id,
            generation=generation,
            fence=fence,
        )

        status_request.additional_properties = d
        return status_request

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
