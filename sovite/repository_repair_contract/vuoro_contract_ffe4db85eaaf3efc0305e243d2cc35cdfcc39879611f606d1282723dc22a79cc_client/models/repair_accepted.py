from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="RepairAccepted")


@_attrs_define
class RepairAccepted:
    """
    Attributes:
        run_id (str):
        parent_run_id (str):
        root_run_id (str):
        repair_number (int):
    """

    run_id: str
    parent_run_id: str
    root_run_id: str
    repair_number: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id

        parent_run_id = self.parent_run_id

        root_run_id = self.root_run_id

        repair_number = self.repair_number

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "root_run_id": root_run_id,
                "repair_number": repair_number,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        run_id = d.pop("run_id")

        parent_run_id = d.pop("parent_run_id")

        root_run_id = d.pop("root_run_id")

        repair_number = d.pop("repair_number")

        repair_accepted = cls(
            run_id=run_id,
            parent_run_id=parent_run_id,
            root_run_id=root_run_id,
            repair_number=repair_number,
        )

        repair_accepted.additional_properties = d
        return repair_accepted

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
