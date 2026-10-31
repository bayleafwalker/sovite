from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.resource_operation import ResourceOperation

T = TypeVar("T", bound="OutputGrantRequest")


@_attrs_define
class OutputGrantRequest:
    """
    Attributes:
        workload_id (str):
        principal_id (str):
        tenant_id (str):
        workspace_id (str):
        domain_id (str):
        target_id (str):
        operation (ResourceOperation):
        policy_revision (str):
    """

    workload_id: str
    principal_id: str
    tenant_id: str
    workspace_id: str
    domain_id: str
    target_id: str
    operation: ResourceOperation
    policy_revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workload_id = self.workload_id

        principal_id = self.principal_id

        tenant_id = self.tenant_id

        workspace_id = self.workspace_id

        domain_id = self.domain_id

        target_id = self.target_id

        operation = self.operation.value

        policy_revision = self.policy_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workload_id": workload_id,
                "principal_id": principal_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "domain_id": domain_id,
                "target_id": target_id,
                "operation": operation,
                "policy_revision": policy_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        workload_id = d.pop("workload_id")

        principal_id = d.pop("principal_id")

        tenant_id = d.pop("tenant_id")

        workspace_id = d.pop("workspace_id")

        domain_id = d.pop("domain_id")

        target_id = d.pop("target_id")

        operation = ResourceOperation(d.pop("operation"))

        policy_revision = d.pop("policy_revision")

        output_grant_request = cls(
            workload_id=workload_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            domain_id=domain_id,
            target_id=target_id,
            operation=operation,
            policy_revision=policy_revision,
        )

        output_grant_request.additional_properties = d
        return output_grant_request

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
