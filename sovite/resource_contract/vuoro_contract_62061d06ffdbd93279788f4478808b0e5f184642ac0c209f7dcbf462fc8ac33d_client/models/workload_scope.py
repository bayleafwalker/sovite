from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

if TYPE_CHECKING:
    from ..models.target_catalog import TargetCatalog


T = TypeVar("T", bound="WorkloadScope")


@_attrs_define
class WorkloadScope:
    """
    Attributes:
        workload_id (str):
        gateway_id (str):
        tenant_id (str):
        workspace_id (str):
        domain_id (str):
        catalog (TargetCatalog):
    """

    workload_id: str
    gateway_id: str
    tenant_id: str
    workspace_id: str
    domain_id: str
    catalog: TargetCatalog
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workload_id = self.workload_id

        gateway_id = self.gateway_id

        tenant_id = self.tenant_id

        workspace_id = self.workspace_id

        domain_id = self.domain_id

        catalog = self.catalog.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workload_id": workload_id,
                "gateway_id": gateway_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "domain_id": domain_id,
                "catalog": catalog,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.target_catalog import TargetCatalog

        d = dict(src_dict)
        workload_id = d.pop("workload_id")

        gateway_id = d.pop("gateway_id")

        tenant_id = d.pop("tenant_id")

        workspace_id = d.pop("workspace_id")

        domain_id = d.pop("domain_id")

        catalog = TargetCatalog.from_dict(d.pop("catalog"))

        workload_scope = cls(
            workload_id=workload_id,
            gateway_id=gateway_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            domain_id=domain_id,
            catalog=catalog,
        )

        workload_scope.additional_properties = d
        return workload_scope

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
