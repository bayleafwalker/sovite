from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.repair_feedback_result import RepairFeedbackResult
from ..models.repair_feedback_schema_version import RepairFeedbackSchemaVersion

T = TypeVar("T", bound="RepairFeedback")


@_attrs_define
class RepairFeedback:
    """
    Attributes:
        schema_version (RepairFeedbackSchemaVersion):
        parent_run_id (str):
        candidate_export_sha256 (str):
        candidate_tip_sha (str):
        result (RepairFeedbackResult):
        feedback (str):
    """

    schema_version: RepairFeedbackSchemaVersion
    parent_run_id: str
    candidate_export_sha256: str
    candidate_tip_sha: str
    result: RepairFeedbackResult
    feedback: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        parent_run_id = self.parent_run_id

        candidate_export_sha256 = self.candidate_export_sha256

        candidate_tip_sha = self.candidate_tip_sha

        result = self.result.value

        feedback = self.feedback

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "parent_run_id": parent_run_id,
                "candidate_export_sha256": candidate_export_sha256,
                "candidate_tip_sha": candidate_tip_sha,
                "result": result,
                "feedback": feedback,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        schema_version = RepairFeedbackSchemaVersion(d.pop("schema_version"))

        parent_run_id = d.pop("parent_run_id")

        candidate_export_sha256 = d.pop("candidate_export_sha256")

        candidate_tip_sha = d.pop("candidate_tip_sha")

        result = RepairFeedbackResult(d.pop("result"))

        feedback = d.pop("feedback")

        repair_feedback = cls(
            schema_version=schema_version,
            parent_run_id=parent_run_id,
            candidate_export_sha256=candidate_export_sha256,
            candidate_tip_sha=candidate_tip_sha,
            result=result,
            feedback=feedback,
        )

        repair_feedback.additional_properties = d
        return repair_feedback

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
