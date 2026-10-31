from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.recovery_captured_kind import RecoveryCapturedKind
from ..models.recovery_captured_outcome import RecoveryCapturedOutcome

T = TypeVar("T", bound="RecoveryCaptured")


@_attrs_define
class RecoveryCaptured:
    """
    Attributes:
        kind (RecoveryCapturedKind):
        run_id (str):
        attempt_id (str):
        workspace_id (str):
        repository_id (str):
        base_sha (str):
        tip_sha (str):
        source_digest (str):
        outcome (RecoveryCapturedOutcome):
        publication_eligible (bool):
        repair_eligible (bool):
    """

    kind: RecoveryCapturedKind
    run_id: str
    attempt_id: str
    workspace_id: str
    repository_id: str
    base_sha: str
    tip_sha: str
    source_digest: str
    outcome: RecoveryCapturedOutcome
    publication_eligible: bool
    repair_eligible: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        run_id = self.run_id

        attempt_id = self.attempt_id

        workspace_id = self.workspace_id

        repository_id = self.repository_id

        base_sha = self.base_sha

        tip_sha = self.tip_sha

        source_digest = self.source_digest

        outcome = self.outcome.value

        publication_eligible = self.publication_eligible

        repair_eligible = self.repair_eligible

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "workspace_id": workspace_id,
                "repository_id": repository_id,
                "base_sha": base_sha,
                "tip_sha": tip_sha,
                "source_digest": source_digest,
                "outcome": outcome,
                "publication_eligible": publication_eligible,
                "repair_eligible": repair_eligible,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        kind = RecoveryCapturedKind(d.pop("kind"))

        run_id = d.pop("run_id")

        attempt_id = d.pop("attempt_id")

        workspace_id = d.pop("workspace_id")

        repository_id = d.pop("repository_id")

        base_sha = d.pop("base_sha")

        tip_sha = d.pop("tip_sha")

        source_digest = d.pop("source_digest")

        outcome = RecoveryCapturedOutcome(d.pop("outcome"))

        publication_eligible = d.pop("publication_eligible")

        repair_eligible = d.pop("repair_eligible")

        recovery_captured = cls(
            kind=kind,
            run_id=run_id,
            attempt_id=attempt_id,
            workspace_id=workspace_id,
            repository_id=repository_id,
            base_sha=base_sha,
            tip_sha=tip_sha,
            source_digest=source_digest,
            outcome=outcome,
            publication_eligible=publication_eligible,
            repair_eligible=repair_eligible,
        )

        recovery_captured.additional_properties = d
        return recovery_captured

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
