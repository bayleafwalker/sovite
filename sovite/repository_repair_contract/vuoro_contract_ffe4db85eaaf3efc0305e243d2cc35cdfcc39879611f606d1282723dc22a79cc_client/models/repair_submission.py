from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.repair_submission_schema_version import RepairSubmissionSchemaVersion

T = TypeVar("T", bound="RepairSubmission")


@_attrs_define
class RepairSubmission:
    """
    Attributes:
        schema_version (RepairSubmissionSchemaVersion):
        parent_run_id (str):
        candidate_sha256 (str):
        feedback_release_id (str):
        preview_id (str):
        max_cost_usd (float):
    """

    schema_version: RepairSubmissionSchemaVersion
    parent_run_id: str
    candidate_sha256: str
    feedback_release_id: str
    preview_id: str
    max_cost_usd: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        parent_run_id = self.parent_run_id

        candidate_sha256 = self.candidate_sha256

        feedback_release_id = self.feedback_release_id

        preview_id = self.preview_id

        max_cost_usd = self.max_cost_usd

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "parent_run_id": parent_run_id,
                "candidate_sha256": candidate_sha256,
                "feedback_release_id": feedback_release_id,
                "preview_id": preview_id,
                "max_cost_usd": max_cost_usd,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        schema_version = RepairSubmissionSchemaVersion(d.pop("schema_version"))

        parent_run_id = d.pop("parent_run_id")

        candidate_sha256 = d.pop("candidate_sha256")

        feedback_release_id = d.pop("feedback_release_id")

        preview_id = d.pop("preview_id")

        max_cost_usd = d.pop("max_cost_usd")

        repair_submission = cls(
            schema_version=schema_version,
            parent_run_id=parent_run_id,
            candidate_sha256=candidate_sha256,
            feedback_release_id=feedback_release_id,
            preview_id=preview_id,
            max_cost_usd=max_cost_usd,
        )

        repair_submission.additional_properties = d
        return repair_submission

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
