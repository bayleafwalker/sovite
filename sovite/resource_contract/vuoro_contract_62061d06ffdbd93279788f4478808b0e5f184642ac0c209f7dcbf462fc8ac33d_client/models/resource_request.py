from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.resource_request_schema_version import ResourceRequestSchemaVersion

if TYPE_CHECKING:
    from ..models.health_selector import HealthSelector
    from ..models.lab_create_selector import LabCreateSelector
    from ..models.lab_observe_selector import LabObserveSelector
    from ..models.lab_stop_selector import LabStopSelector


T = TypeVar("T", bound="ResourceRequest")


@_attrs_define
class ResourceRequest:
    """
    Attributes:
        schema_version (ResourceRequestSchemaVersion):
        workspace_id (str):
        resource_domain_id (str):
        resource (HealthSelector | LabCreateSelector | LabObserveSelector | LabStopSelector):
        target_revision (str):
        execution_deadline (float):
    """

    schema_version: ResourceRequestSchemaVersion
    workspace_id: str
    resource_domain_id: str
    resource: HealthSelector | LabCreateSelector | LabObserveSelector | LabStopSelector
    target_revision: str
    execution_deadline: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.health_selector import HealthSelector
        from ..models.lab_create_selector import LabCreateSelector
        from ..models.lab_observe_selector import LabObserveSelector

        schema_version = self.schema_version.value

        workspace_id = self.workspace_id

        resource_domain_id = self.resource_domain_id

        resource: dict[str, Any]
        if (
            isinstance(self.resource, HealthSelector)
            or isinstance(self.resource, LabCreateSelector)
            or isinstance(self.resource, LabObserveSelector)
        ):
            resource = self.resource.to_dict()
        else:
            resource = self.resource.to_dict()

        target_revision = self.target_revision

        execution_deadline = self.execution_deadline

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "workspace_id": workspace_id,
                "resource_domain_id": resource_domain_id,
                "resource": resource,
                "target_revision": target_revision,
                "execution_deadline": execution_deadline,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.health_selector import HealthSelector
        from ..models.lab_create_selector import LabCreateSelector
        from ..models.lab_observe_selector import LabObserveSelector
        from ..models.lab_stop_selector import LabStopSelector

        d = dict(src_dict)
        schema_version = ResourceRequestSchemaVersion(d.pop("schema_version"))

        workspace_id = d.pop("workspace_id")

        resource_domain_id = d.pop("resource_domain_id")

        def _parse_resource(
            data: object,
        ) -> HealthSelector | LabCreateSelector | LabObserveSelector | LabStopSelector:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_resource_selector_type_0 = HealthSelector.from_dict(
                    data
                )

                return componentsschemas_resource_selector_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_resource_selector_type_1 = (
                    LabCreateSelector.from_dict(data)
                )

                return componentsschemas_resource_selector_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_resource_selector_type_2 = (
                    LabObserveSelector.from_dict(data)
                )

                return componentsschemas_resource_selector_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_resource_selector_type_3 = LabStopSelector.from_dict(data)

            return componentsschemas_resource_selector_type_3

        resource = _parse_resource(d.pop("resource"))

        target_revision = d.pop("target_revision")

        execution_deadline = d.pop("execution_deadline")

        resource_request = cls(
            schema_version=schema_version,
            workspace_id=workspace_id,
            resource_domain_id=resource_domain_id,
            resource=resource,
            target_revision=target_revision,
            execution_deadline=execution_deadline,
        )

        resource_request.additional_properties = d
        return resource_request

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
