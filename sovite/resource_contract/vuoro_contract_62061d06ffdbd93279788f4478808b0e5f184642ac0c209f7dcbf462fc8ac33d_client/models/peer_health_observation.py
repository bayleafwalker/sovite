from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.peer_health_observation_health_scope import (
    PeerHealthObservationHealthScope,
)
from ..models.peer_health_observation_operation import PeerHealthObservationOperation
from ..models.peer_health_observation_schema_version import (
    PeerHealthObservationSchemaVersion,
)

T = TypeVar("T", bound="PeerHealthObservation")


@_attrs_define
class PeerHealthObservation:
    """
    Attributes:
        schema_version (PeerHealthObservationSchemaVersion):
        workspace_id (str):
        resource_domain_id (str):
        target_id (str):
        operation (PeerHealthObservationOperation):
        health_scope (PeerHealthObservationHealthScope):
        daemon_responsive (bool):
    """

    schema_version: PeerHealthObservationSchemaVersion
    workspace_id: str
    resource_domain_id: str
    target_id: str
    operation: PeerHealthObservationOperation
    health_scope: PeerHealthObservationHealthScope
    daemon_responsive: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        workspace_id = self.workspace_id

        resource_domain_id = self.resource_domain_id

        target_id = self.target_id

        operation = self.operation.value

        health_scope = self.health_scope.value

        daemon_responsive = self.daemon_responsive

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "workspace_id": workspace_id,
                "resource_domain_id": resource_domain_id,
                "target_id": target_id,
                "operation": operation,
                "health_scope": health_scope,
                "daemon_responsive": daemon_responsive,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        schema_version = PeerHealthObservationSchemaVersion(d.pop("schema_version"))

        workspace_id = d.pop("workspace_id")

        resource_domain_id = d.pop("resource_domain_id")

        target_id = d.pop("target_id")

        operation = PeerHealthObservationOperation(d.pop("operation"))

        health_scope = PeerHealthObservationHealthScope(d.pop("health_scope"))

        daemon_responsive = d.pop("daemon_responsive")

        peer_health_observation = cls(
            schema_version=schema_version,
            workspace_id=workspace_id,
            resource_domain_id=resource_domain_id,
            target_id=target_id,
            operation=operation,
            health_scope=health_scope,
            daemon_responsive=daemon_responsive,
        )

        peer_health_observation.additional_properties = d
        return peer_health_observation

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
