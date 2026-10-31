from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="RecoveryReleased")


@_attrs_define
class RecoveryReleased:
    """
    Attributes:
        release_id (str):
        output_class (str):
        output_digest (str):
        contract_revision (str):
        destinations (list[str]):
    """

    release_id: str
    output_class: str
    output_digest: str
    contract_revision: str
    destinations: list[str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        release_id = self.release_id

        output_class = self.output_class

        output_digest = self.output_digest

        contract_revision = self.contract_revision

        destinations = self.destinations

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "release_id": release_id,
                "output_class": output_class,
                "output_digest": output_digest,
                "contract_revision": contract_revision,
                "destinations": destinations,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        release_id = d.pop("release_id")

        output_class = d.pop("output_class")

        output_digest = d.pop("output_digest")

        contract_revision = d.pop("contract_revision")

        destinations = cast(list[str], d.pop("destinations"))

        recovery_released = cls(
            release_id=release_id,
            output_class=output_class,
            output_digest=output_digest,
            contract_revision=contract_revision,
            destinations=destinations,
        )

        recovery_released.additional_properties = d
        return recovery_released

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
