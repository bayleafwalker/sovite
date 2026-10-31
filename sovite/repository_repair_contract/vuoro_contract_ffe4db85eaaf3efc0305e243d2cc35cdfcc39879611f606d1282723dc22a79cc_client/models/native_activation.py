from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.native_activation_schema_version import NativeActivationSchemaVersion

T = TypeVar("T", bound="NativeActivation")


@_attrs_define
class NativeActivation:
    """
    Attributes:
        schema_version (NativeActivationSchemaVersion):
        job_id (str):
        attempt_id (str):
        workspace_id (str):
        repository_id (str):
        principal_id (str):
        generation (str):
        fence_sha256 (str):
        packet_sha256 (str):
        executor_id (str):
        publication_revision (str):
        qualification_ref (str):
        vm_id (str):
        deadline (float):
        lease_until (float):
    """

    schema_version: NativeActivationSchemaVersion
    job_id: str
    attempt_id: str
    workspace_id: str
    repository_id: str
    principal_id: str
    generation: str
    fence_sha256: str
    packet_sha256: str
    executor_id: str
    publication_revision: str
    qualification_ref: str
    vm_id: str
    deadline: float
    lease_until: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version.value

        job_id = self.job_id

        attempt_id = self.attempt_id

        workspace_id = self.workspace_id

        repository_id = self.repository_id

        principal_id = self.principal_id

        generation = self.generation

        fence_sha256 = self.fence_sha256

        packet_sha256 = self.packet_sha256

        executor_id = self.executor_id

        publication_revision = self.publication_revision

        qualification_ref = self.qualification_ref

        vm_id = self.vm_id

        deadline = self.deadline

        lease_until = self.lease_until

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "workspace_id": workspace_id,
                "repository_id": repository_id,
                "principal_id": principal_id,
                "generation": generation,
                "fence_sha256": fence_sha256,
                "packet_sha256": packet_sha256,
                "executor_id": executor_id,
                "publication_revision": publication_revision,
                "qualification_ref": qualification_ref,
                "vm_id": vm_id,
                "deadline": deadline,
                "lease_until": lease_until,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        schema_version = NativeActivationSchemaVersion(d.pop("schema_version"))

        job_id = d.pop("job_id")

        attempt_id = d.pop("attempt_id")

        workspace_id = d.pop("workspace_id")

        repository_id = d.pop("repository_id")

        principal_id = d.pop("principal_id")

        generation = d.pop("generation")

        fence_sha256 = d.pop("fence_sha256")

        packet_sha256 = d.pop("packet_sha256")

        executor_id = d.pop("executor_id")

        publication_revision = d.pop("publication_revision")

        qualification_ref = d.pop("qualification_ref")

        vm_id = d.pop("vm_id")

        deadline = d.pop("deadline")

        lease_until = d.pop("lease_until")

        native_activation = cls(
            schema_version=schema_version,
            job_id=job_id,
            attempt_id=attempt_id,
            workspace_id=workspace_id,
            repository_id=repository_id,
            principal_id=principal_id,
            generation=generation,
            fence_sha256=fence_sha256,
            packet_sha256=packet_sha256,
            executor_id=executor_id,
            publication_revision=publication_revision,
            qualification_ref=qualification_ref,
            vm_id=vm_id,
            deadline=deadline,
            lease_until=lease_until,
        )

        native_activation.additional_properties = d
        return native_activation

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
