from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.gateway_request_audience import GatewayRequestAudience
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.create_binding import CreateBinding
    from ..models.lab_entry import LabEntry
    from ..models.peer_health_entry import PeerHealthEntry
    from ..models.resource_request import ResourceRequest


T = TypeVar("T", bound="GatewayRequest")


@_attrs_define
class GatewayRequest:
    """
    Attributes:
        request_id (str):
        job_id (str):
        principal_id (str):
        audience (GatewayRequestAudience):
        instance_id (str):
        generation (str):
        gateway_id (str):
        tenant_id (str):
        workspace_id (str):
        domain_id (str):
        workload (str):
        fence_fingerprint (str):
        request (ResourceRequest):
        target (LabEntry | PeerHealthEntry):
        created_at (float):
        deadline (float):
        create (CreateBinding | Unset):
    """

    request_id: str
    job_id: str
    principal_id: str
    audience: GatewayRequestAudience
    instance_id: str
    generation: str
    gateway_id: str
    tenant_id: str
    workspace_id: str
    domain_id: str
    workload: str
    fence_fingerprint: str
    request: ResourceRequest
    target: LabEntry | PeerHealthEntry
    created_at: float
    deadline: float
    create: CreateBinding | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.peer_health_entry import PeerHealthEntry

        request_id = self.request_id

        job_id = self.job_id

        principal_id = self.principal_id

        audience = self.audience.value

        instance_id = self.instance_id

        generation = self.generation

        gateway_id = self.gateway_id

        tenant_id = self.tenant_id

        workspace_id = self.workspace_id

        domain_id = self.domain_id

        workload = self.workload

        fence_fingerprint = self.fence_fingerprint

        request = self.request.to_dict()

        target: dict[str, Any]
        if isinstance(self.target, PeerHealthEntry):
            target = self.target.to_dict()
        else:
            target = self.target.to_dict()

        created_at = self.created_at

        deadline = self.deadline

        create: dict[str, Any] | Unset = UNSET
        if not isinstance(self.create, Unset):
            create = self.create.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "job_id": job_id,
                "principal_id": principal_id,
                "audience": audience,
                "instance_id": instance_id,
                "generation": generation,
                "gateway_id": gateway_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "domain_id": domain_id,
                "workload": workload,
                "fence_fingerprint": fence_fingerprint,
                "request": request,
                "target": target,
                "created_at": created_at,
                "deadline": deadline,
            }
        )
        if create is not UNSET:
            field_dict["create"] = create

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.create_binding import CreateBinding
        from ..models.lab_entry import LabEntry
        from ..models.peer_health_entry import PeerHealthEntry
        from ..models.resource_request import ResourceRequest

        d = dict(src_dict)
        request_id = d.pop("request_id")

        job_id = d.pop("job_id")

        principal_id = d.pop("principal_id")

        audience = GatewayRequestAudience(d.pop("audience"))

        instance_id = d.pop("instance_id")

        generation = d.pop("generation")

        gateway_id = d.pop("gateway_id")

        tenant_id = d.pop("tenant_id")

        workspace_id = d.pop("workspace_id")

        domain_id = d.pop("domain_id")

        workload = d.pop("workload")

        fence_fingerprint = d.pop("fence_fingerprint")

        request = ResourceRequest.from_dict(d.pop("request"))

        def _parse_target(data: object) -> LabEntry | PeerHealthEntry:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_resource_entry_type_0 = PeerHealthEntry.from_dict(
                    data
                )

                return componentsschemas_resource_entry_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_resource_entry_type_1 = LabEntry.from_dict(data)

            return componentsschemas_resource_entry_type_1

        target = _parse_target(d.pop("target"))

        created_at = d.pop("created_at")

        deadline = d.pop("deadline")

        _create = d.pop("create", UNSET)
        create: CreateBinding | Unset
        if isinstance(_create, Unset):
            create = UNSET
        else:
            create = CreateBinding.from_dict(_create)

        gateway_request = cls(
            request_id=request_id,
            job_id=job_id,
            principal_id=principal_id,
            audience=audience,
            instance_id=instance_id,
            generation=generation,
            gateway_id=gateway_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            domain_id=domain_id,
            workload=workload,
            fence_fingerprint=fence_fingerprint,
            request=request,
            target=target,
            created_at=created_at,
            deadline=deadline,
            create=create,
        )

        gateway_request.additional_properties = d
        return gateway_request

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
