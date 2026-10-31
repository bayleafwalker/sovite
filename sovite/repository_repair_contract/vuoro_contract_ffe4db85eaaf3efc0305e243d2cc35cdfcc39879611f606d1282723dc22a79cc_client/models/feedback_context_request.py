from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="FeedbackContextRequest")


@_attrs_define
class FeedbackContextRequest:
    """
    Attributes:
        parent_run_id (str):
        feedback_contract_id (str):
    """

    parent_run_id: str
    feedback_contract_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        parent_run_id = self.parent_run_id

        feedback_contract_id = self.feedback_contract_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "parent_run_id": parent_run_id,
                "feedback_contract_id": feedback_contract_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        parent_run_id = d.pop("parent_run_id")

        feedback_contract_id = d.pop("feedback_contract_id")

        feedback_context_request = cls(
            parent_run_id=parent_run_id,
            feedback_contract_id=feedback_contract_id,
        )

        feedback_context_request.additional_properties = d
        return feedback_context_request

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
