from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.recovery_release_context_kind import RecoveryReleaseContextKind

T = TypeVar("T", bound="RecoveryReleaseContext")


@_attrs_define
class RecoveryReleaseContext:
    """
    Attributes:
        kind (RecoveryReleaseContextKind):
        run_id (str):
        attempt_id (str):
        source_digest (str):
        contract_id (str):
        contract_revision (str):
        expected_generation (str):
        procedure_digest (str):
        output_class (str):
        destinations (list[str]):
        publication_eligible (bool):
        repair_eligible (bool):
    """

    kind: RecoveryReleaseContextKind
    run_id: str
    attempt_id: str
    source_digest: str
    contract_id: str
    contract_revision: str
    expected_generation: str
    procedure_digest: str
    output_class: str
    destinations: list[str]
    publication_eligible: bool
    repair_eligible: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        run_id = self.run_id

        attempt_id = self.attempt_id

        source_digest = self.source_digest

        contract_id = self.contract_id

        contract_revision = self.contract_revision

        expected_generation = self.expected_generation

        procedure_digest = self.procedure_digest

        output_class = self.output_class

        destinations = self.destinations

        publication_eligible = self.publication_eligible

        repair_eligible = self.repair_eligible

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "source_digest": source_digest,
                "contract_id": contract_id,
                "contract_revision": contract_revision,
                "expected_generation": expected_generation,
                "procedure_digest": procedure_digest,
                "output_class": output_class,
                "destinations": destinations,
                "publication_eligible": publication_eligible,
                "repair_eligible": repair_eligible,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        kind = RecoveryReleaseContextKind(d.pop("kind"))

        run_id = d.pop("run_id")

        attempt_id = d.pop("attempt_id")

        source_digest = d.pop("source_digest")

        contract_id = d.pop("contract_id")

        contract_revision = d.pop("contract_revision")

        expected_generation = d.pop("expected_generation")

        procedure_digest = d.pop("procedure_digest")

        output_class = d.pop("output_class")

        destinations = cast(list[str], d.pop("destinations"))

        publication_eligible = d.pop("publication_eligible")

        repair_eligible = d.pop("repair_eligible")

        recovery_release_context = cls(
            kind=kind,
            run_id=run_id,
            attempt_id=attempt_id,
            source_digest=source_digest,
            contract_id=contract_id,
            contract_revision=contract_revision,
            expected_generation=expected_generation,
            procedure_digest=procedure_digest,
            output_class=output_class,
            destinations=destinations,
            publication_eligible=publication_eligible,
            repair_eligible=repair_eligible,
        )

        recovery_release_context.additional_properties = d
        return recovery_release_context

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
