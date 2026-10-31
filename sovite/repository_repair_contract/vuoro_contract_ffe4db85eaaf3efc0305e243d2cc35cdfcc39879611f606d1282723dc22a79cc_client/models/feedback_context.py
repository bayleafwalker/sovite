from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="FeedbackContext")


@_attrs_define
class FeedbackContext:
    """
    Attributes:
        parent_run_id (str):
        source_digest (str):
        contract_id (str):
        contract_revision (str):
        expected_generation (str):
        candidate_sha256 (str):
        candidate_tip_sha (str):
    """

    parent_run_id: str
    source_digest: str
    contract_id: str
    contract_revision: str
    expected_generation: str
    candidate_sha256: str
    candidate_tip_sha: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        parent_run_id = self.parent_run_id

        source_digest = self.source_digest

        contract_id = self.contract_id

        contract_revision = self.contract_revision

        expected_generation = self.expected_generation

        candidate_sha256 = self.candidate_sha256

        candidate_tip_sha = self.candidate_tip_sha

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "parent_run_id": parent_run_id,
                "source_digest": source_digest,
                "contract_id": contract_id,
                "contract_revision": contract_revision,
                "expected_generation": expected_generation,
                "candidate_sha256": candidate_sha256,
                "candidate_tip_sha": candidate_tip_sha,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        parent_run_id = d.pop("parent_run_id")

        source_digest = d.pop("source_digest")

        contract_id = d.pop("contract_id")

        contract_revision = d.pop("contract_revision")

        expected_generation = d.pop("expected_generation")

        candidate_sha256 = d.pop("candidate_sha256")

        candidate_tip_sha = d.pop("candidate_tip_sha")

        feedback_context = cls(
            parent_run_id=parent_run_id,
            source_digest=source_digest,
            contract_id=contract_id,
            contract_revision=contract_revision,
            expected_generation=expected_generation,
            candidate_sha256=candidate_sha256,
            candidate_tip_sha=candidate_tip_sha,
        )

        feedback_context.additional_properties = d
        return feedback_context

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
