from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.c2_profile_output_class import C2ProfileOutputClass
from ..models.c2_profile_source_class import C2ProfileSourceClass

T = TypeVar("T", bound="C2Profile")


@_attrs_define
class C2Profile:
    """
    Attributes:
        source_class (C2ProfileSourceClass):
        output_class (C2ProfileOutputClass):
        destination (str):
    """

    source_class: C2ProfileSourceClass
    output_class: C2ProfileOutputClass
    destination: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source_class = self.source_class.value

        output_class = self.output_class.value

        destination = self.destination

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source_class": source_class,
                "output_class": output_class,
                "destination": destination,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        source_class = C2ProfileSourceClass(d.pop("source_class"))

        output_class = C2ProfileOutputClass(d.pop("output_class"))

        destination = d.pop("destination")

        c2_profile = cls(
            source_class=source_class,
            output_class=output_class,
            destination=destination,
        )

        c2_profile.additional_properties = d
        return c2_profile

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
