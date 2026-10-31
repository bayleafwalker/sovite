from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.health_selector_operation import HealthSelectorOperation

T = TypeVar("T", bound="HealthSelector")


@_attrs_define
class HealthSelector:
    """
    Attributes:
        operation (HealthSelectorOperation):
        target_id (str):
    """

    operation: HealthSelectorOperation
    target_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        operation = self.operation.value

        target_id = self.target_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "operation": operation,
                "target_id": target_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        operation = HealthSelectorOperation(d.pop("operation"))

        target_id = d.pop("target_id")

        health_selector = cls(
            operation=operation,
            target_id=target_id,
        )

        health_selector.additional_properties = d
        return health_selector

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
