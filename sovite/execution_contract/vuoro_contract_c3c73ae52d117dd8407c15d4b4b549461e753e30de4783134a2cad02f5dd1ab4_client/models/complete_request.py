from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.worker_outcome import WorkerOutcome
from ..types import UNSET, Unset

T = TypeVar("T", bound="CompleteRequest")


@_attrs_define
class CompleteRequest:
    """
    Attributes:
        instance_id (str):
        job_id (str):
        generation (str):
        fence (str):
        operation_id (str):
        outcome (WorkerOutcome):
        result_json (str | Unset):
        error_code (None | str | Unset):
    """

    instance_id: str
    job_id: str
    generation: str
    fence: str
    operation_id: str
    outcome: WorkerOutcome
    result_json: str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        instance_id = self.instance_id

        job_id = self.job_id

        generation = self.generation

        fence = self.fence

        operation_id = self.operation_id

        outcome = self.outcome.value

        result_json = self.result_json

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "instance_id": instance_id,
                "job_id": job_id,
                "generation": generation,
                "fence": fence,
                "operation_id": operation_id,
                "outcome": outcome,
            }
        )
        if result_json is not UNSET:
            field_dict["result_json"] = result_json
        if error_code is not UNSET:
            field_dict["error_code"] = error_code

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        instance_id = d.pop("instance_id")

        job_id = d.pop("job_id")

        generation = d.pop("generation")

        fence = d.pop("fence")

        operation_id = d.pop("operation_id")

        outcome = WorkerOutcome(d.pop("outcome"))

        result_json = d.pop("result_json", UNSET)

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        complete_request = cls(
            instance_id=instance_id,
            job_id=job_id,
            generation=generation,
            fence=fence,
            operation_id=operation_id,
            outcome=outcome,
            result_json=result_json,
            error_code=error_code,
        )

        complete_request.additional_properties = d
        return complete_request

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
