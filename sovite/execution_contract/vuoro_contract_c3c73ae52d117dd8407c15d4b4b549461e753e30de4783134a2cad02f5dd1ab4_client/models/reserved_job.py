from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

if TYPE_CHECKING:
    from ..models.agent_http_recipe import AgentHttpRecipe


T = TypeVar("T", bound="ReservedJob")


@_attrs_define
class ReservedJob:
    """
    Attributes:
        job_id (str):
        generation (str):
        fence (str):
        lease_until (float):
        task (str):
        recipe (AgentHttpRecipe):
    """

    job_id: str
    generation: str
    fence: str
    lease_until: float
    task: str
    recipe: AgentHttpRecipe
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        job_id = self.job_id

        generation = self.generation

        fence = self.fence

        lease_until = self.lease_until

        task = self.task

        recipe = self.recipe.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "job_id": job_id,
                "generation": generation,
                "fence": fence,
                "lease_until": lease_until,
                "task": task,
                "recipe": recipe,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.agent_http_recipe import AgentHttpRecipe

        d = dict(src_dict)
        job_id = d.pop("job_id")

        generation = d.pop("generation")

        fence = d.pop("fence")

        lease_until = d.pop("lease_until")

        task = d.pop("task")

        recipe = AgentHttpRecipe.from_dict(d.pop("recipe"))

        reserved_job = cls(
            job_id=job_id,
            generation=generation,
            fence=fence,
            lease_until=lease_until,
            task=task,
            recipe=recipe,
        )

        reserved_job.additional_properties = d
        return reserved_job

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
