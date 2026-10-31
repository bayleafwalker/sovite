from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.gateway_policy_audience import GatewayPolicyAudience
from ..models.gateway_policy_schema_version import GatewayPolicySchemaVersion

if TYPE_CHECKING:
    from ..models.target_catalog import TargetCatalog


T = TypeVar("T", bound="GatewayPolicy")


@_attrs_define
class GatewayPolicy:
    """
    Attributes:
        schema_version (GatewayPolicySchemaVersion):
        gateway_id (str):
        audience (GatewayPolicyAudience):
        tenant_id (str):
        principal_id (str):
        workspace_id (str):
        domain_id (str):
        expires_at (float):
        catalog (TargetCatalog):
        release_policy_revision (str):
    """

    schema_version: GatewayPolicySchemaVersion
    gateway_id: str
    audience: GatewayPolicyAudience
    tenant_id: str
    principal_id: str
    workspace_id: str
    domain_id: str
    expires_at: float
    catalog: TargetCatalog
    release_policy_revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        gateway_id = self.gateway_id

        audience = self.audience.value

        tenant_id = self.tenant_id

        principal_id = self.principal_id

        workspace_id = self.workspace_id

        domain_id = self.domain_id

        expires_at = self.expires_at

        catalog = self.catalog.to_dict()

        release_policy_revision = self.release_policy_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "gateway_id": gateway_id,
                "audience": audience,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "workspace_id": workspace_id,
                "domain_id": domain_id,
                "expires_at": expires_at,
                "catalog": catalog,
                "release_policy_revision": release_policy_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.target_catalog import TargetCatalog

        d = dict(src_dict)
        schema_version = GatewayPolicySchemaVersion(d.pop("schema_version"))

        gateway_id = d.pop("gateway_id")

        audience = GatewayPolicyAudience(d.pop("audience"))

        tenant_id = d.pop("tenant_id")

        principal_id = d.pop("principal_id")

        workspace_id = d.pop("workspace_id")

        domain_id = d.pop("domain_id")

        expires_at = d.pop("expires_at")

        catalog = TargetCatalog.from_dict(d.pop("catalog"))

        release_policy_revision = d.pop("release_policy_revision")

        gateway_policy = cls(
            schema_version=schema_version,
            gateway_id=gateway_id,
            audience=audience,
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            domain_id=domain_id,
            expires_at=expires_at,
            catalog=catalog,
            release_policy_revision=release_policy_revision,
        )

        gateway_policy.additional_properties = d
        return gateway_policy

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
