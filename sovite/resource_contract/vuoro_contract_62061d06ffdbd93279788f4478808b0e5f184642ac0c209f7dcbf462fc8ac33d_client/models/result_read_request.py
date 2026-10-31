from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

if TYPE_CHECKING:
    from ..models.execution_context import ExecutionContext


T = TypeVar("T", bound="ResultReadRequest")


@_attrs_define
class ResultReadRequest:
    """
    Attributes:
        execution (ExecutionContext):
        request_id (str):
    """

    execution: ExecutionContext
    request_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        execution = self.execution.to_dict()

        request_id = self.request_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "execution": execution,
                "request_id": request_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.execution_context import ExecutionContext

        d = dict(src_dict)
        execution = ExecutionContext.from_dict(d.pop("execution"))

        request_id = d.pop("request_id")

        result_read_request = cls(
            execution=execution,
            request_id=request_id,
        )

        result_read_request.additional_properties = d
        return result_read_request

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
