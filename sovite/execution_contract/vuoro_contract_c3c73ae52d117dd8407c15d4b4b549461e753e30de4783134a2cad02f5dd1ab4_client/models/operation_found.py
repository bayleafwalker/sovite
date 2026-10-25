from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.operation_found_operation_kind import OperationFoundOperationKind
from ..models.operation_found_result_state import OperationFoundResultState
from ..models.operation_found_state import OperationFoundState

T = TypeVar("T", bound="OperationFound")


@_attrs_define
class OperationFound:
    """
    Attributes:
        state (OperationFoundState):
        operation_id (str):
        operation_kind (OperationFoundOperationKind):
        job_id (str):
        fence (str):
        result_state (OperationFoundResultState):
    """

    state: OperationFoundState
    operation_id: str
    operation_kind: OperationFoundOperationKind
    job_id: str
    fence: str
    result_state: OperationFoundResultState
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        operation_id = self.operation_id

        operation_kind = self.operation_kind.value

        job_id = self.job_id

        fence = self.fence

        result_state = self.result_state.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "job_id": job_id,
                "fence": fence,
                "result_state": result_state,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        state = OperationFoundState(d.pop("state"))

        operation_id = d.pop("operation_id")

        operation_kind = OperationFoundOperationKind(d.pop("operation_kind"))

        job_id = d.pop("job_id")

        fence = d.pop("fence")

        result_state = OperationFoundResultState(d.pop("result_state"))

        operation_found = cls(
            state=state,
            operation_id=operation_id,
            operation_kind=operation_kind,
            job_id=job_id,
            fence=fence,
            result_state=result_state,
        )

        operation_found.additional_properties = d
        return operation_found

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
