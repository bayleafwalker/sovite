from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.completion_state import CompletionState
from ..types import UNSET, Unset

T = TypeVar("T", bound="Completion")


@_attrs_define
class Completion:
    """
    Attributes:
        state (CompletionState):
        job_id (str):
        generation (str):
        fence (str):
        error_code (None | str | Unset):
        artifact_sha256 (str | Unset):
    """

    state: CompletionState
    job_id: str
    generation: str
    fence: str
    error_code: None | str | Unset = UNSET
    artifact_sha256: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        job_id = self.job_id

        generation = self.generation

        fence = self.fence

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        artifact_sha256 = self.artifact_sha256

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "job_id": job_id,
                "generation": generation,
                "fence": fence,
            }
        )
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if artifact_sha256 is not UNSET:
            field_dict["artifact_sha256"] = artifact_sha256

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        state = CompletionState(d.pop("state"))

        job_id = d.pop("job_id")

        generation = d.pop("generation")

        fence = d.pop("fence")

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        artifact_sha256 = d.pop("artifact_sha256", UNSET)

        completion = cls(
            state=state,
            job_id=job_id,
            generation=generation,
            fence=fence,
            error_code=error_code,
            artifact_sha256=artifact_sha256,
        )

        completion.additional_properties = d
        return completion

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
