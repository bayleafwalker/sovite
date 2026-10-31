from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.peer_health_configuration_kind import PeerHealthConfigurationKind
from ..models.peer_health_configuration_method import PeerHealthConfigurationMethod
from ..models.peer_health_configuration_path import PeerHealthConfigurationPath

T = TypeVar("T", bound="PeerHealthConfiguration")


@_attrs_define
class PeerHealthConfiguration:
    """
    Attributes:
        kind (PeerHealthConfigurationKind):
        target_id (str):
        origin (str):
        method (PeerHealthConfigurationMethod):
        path (PeerHealthConfigurationPath):
        ca_file (str):
        ca_sha256 (str):
    """

    kind: PeerHealthConfigurationKind
    target_id: str
    origin: str
    method: PeerHealthConfigurationMethod
    path: PeerHealthConfigurationPath
    ca_file: str
    ca_sha256: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        target_id = self.target_id

        origin = self.origin

        method = self.method.value

        path = self.path.value

        ca_file = self.ca_file

        ca_sha256 = self.ca_sha256

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "target_id": target_id,
                "origin": origin,
                "method": method,
                "path": path,
                "ca_file": ca_file,
                "ca_sha256": ca_sha256,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        kind = PeerHealthConfigurationKind(d.pop("kind"))

        target_id = d.pop("target_id")

        origin = d.pop("origin")

        method = PeerHealthConfigurationMethod(d.pop("method"))

        path = PeerHealthConfigurationPath(d.pop("path"))

        ca_file = d.pop("ca_file")

        ca_sha256 = d.pop("ca_sha256")

        peer_health_configuration = cls(
            kind=kind,
            target_id=target_id,
            origin=origin,
            method=method,
            path=path,
            ca_file=ca_file,
            ca_sha256=ca_sha256,
        )

        peer_health_configuration.additional_properties = d
        return peer_health_configuration

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
