from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.resource_result_output_class import ResourceResultOutputClass

if TYPE_CHECKING:
    from ..models.lab_created import LabCreated
    from ..models.lab_observed import LabObserved
    from ..models.lab_stopped import LabStopped
    from ..models.peer_health_observation import PeerHealthObservation


T = TypeVar("T", bound="ResourceResult")


@_attrs_define
class ResourceResult:
    """
    Attributes:
        observation (LabCreated | LabObserved | LabStopped | PeerHealthObservation):
        sha256 (str):
        output_class (ResourceResultOutputClass):
        destination (str):
        policy_revision (str):
    """

    observation: LabCreated | LabObserved | LabStopped | PeerHealthObservation
    sha256: str
    output_class: ResourceResultOutputClass
    destination: str
    policy_revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.lab_created import LabCreated
        from ..models.lab_observed import LabObserved
        from ..models.peer_health_observation import (
            PeerHealthObservation,
        )

        observation: dict[str, Any]
        if (
            isinstance(self.observation, PeerHealthObservation)
            or isinstance(self.observation, LabCreated)
            or isinstance(self.observation, LabObserved)
        ):
            observation = self.observation.to_dict()
        else:
            observation = self.observation.to_dict()

        sha256 = self.sha256

        output_class = self.output_class.value

        destination = self.destination

        policy_revision = self.policy_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "observation": observation,
                "sha256": sha256,
                "output_class": output_class,
                "destination": destination,
                "policy_revision": policy_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.lab_created import LabCreated
        from ..models.lab_observed import LabObserved
        from ..models.lab_stopped import LabStopped
        from ..models.peer_health_observation import (
            PeerHealthObservation,
        )

        d = dict(src_dict)

        def _parse_observation(
            data: object,
        ) -> LabCreated | LabObserved | LabStopped | PeerHealthObservation:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_observation_type_0 = PeerHealthObservation.from_dict(
                    data
                )

                return componentsschemas_observation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_observation_type_1 = LabCreated.from_dict(data)

                return componentsschemas_observation_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_observation_type_2 = LabObserved.from_dict(data)

                return componentsschemas_observation_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_observation_type_3 = LabStopped.from_dict(data)

            return componentsschemas_observation_type_3

        observation = _parse_observation(d.pop("observation"))

        sha256 = d.pop("sha256")

        output_class = ResourceResultOutputClass(d.pop("output_class"))

        destination = d.pop("destination")

        policy_revision = d.pop("policy_revision")

        resource_result = cls(
            observation=observation,
            sha256=sha256,
            output_class=output_class,
            destination=destination,
            policy_revision=policy_revision,
        )

        resource_result.additional_properties = d
        return resource_result

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
