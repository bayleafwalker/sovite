from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

T = TypeVar("T", bound="CreateBinding")


@_attrs_define
class CreateBinding:
    """
    Attributes:
        create_request_id (str):
        principal_id (str):
        gateway_id (str):
        tenant_id (str):
        workspace_id (str):
        domain_id (str):
        target_id (str):
        target_revision (str):
        created_at (float):
        execution_deadline (float):
    """

    create_request_id: str
    principal_id: str
    gateway_id: str
    tenant_id: str
    workspace_id: str
    domain_id: str
    target_id: str
    target_revision: str
    created_at: float
    execution_deadline: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        create_request_id = self.create_request_id

        principal_id = self.principal_id

        gateway_id = self.gateway_id

        tenant_id = self.tenant_id

        workspace_id = self.workspace_id

        domain_id = self.domain_id

        target_id = self.target_id

        target_revision = self.target_revision

        created_at = self.created_at

        execution_deadline = self.execution_deadline

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "create_request_id": create_request_id,
                "principal_id": principal_id,
                "gateway_id": gateway_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "domain_id": domain_id,
                "target_id": target_id,
                "target_revision": target_revision,
                "created_at": created_at,
                "execution_deadline": execution_deadline,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        create_request_id = d.pop("create_request_id")

        principal_id = d.pop("principal_id")

        gateway_id = d.pop("gateway_id")

        tenant_id = d.pop("tenant_id")

        workspace_id = d.pop("workspace_id")

        domain_id = d.pop("domain_id")

        target_id = d.pop("target_id")

        target_revision = d.pop("target_revision")

        created_at = d.pop("created_at")

        execution_deadline = d.pop("execution_deadline")

        create_binding = cls(
            create_request_id=create_request_id,
            principal_id=principal_id,
            gateway_id=gateway_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            domain_id=domain_id,
            target_id=target_id,
            target_revision=target_revision,
            created_at=created_at,
            execution_deadline=execution_deadline,
        )

        create_binding.additional_properties = d
        return create_binding

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
