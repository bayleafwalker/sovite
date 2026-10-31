from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.peer_health_entry_kind import PeerHealthEntryKind

if TYPE_CHECKING:
    from ..models.c0_profile import C0Profile
    from ..models.c1_profile import C1Profile
    from ..models.c2_profile import C2Profile


T = TypeVar("T", bound="PeerHealthEntry")


@_attrs_define
class PeerHealthEntry:
    """
    Attributes:
        kind (PeerHealthEntryKind):
        target_id (str):
        target_revision (str):
        read_profile (C0Profile | C1Profile | C2Profile):
    """

    kind: PeerHealthEntryKind
    target_id: str
    target_revision: str
    read_profile: C0Profile | C1Profile | C2Profile
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.c0_profile import C0Profile
        from ..models.c1_profile import C1Profile

        kind = self.kind.value

        target_id = self.target_id

        target_revision = self.target_revision

        read_profile: dict[str, Any]
        if isinstance(self.read_profile, C0Profile) or isinstance(
            self.read_profile, C1Profile
        ):
            read_profile = self.read_profile.to_dict()
        else:
            read_profile = self.read_profile.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "target_id": target_id,
                "target_revision": target_revision,
                "read_profile": read_profile,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.c0_profile import C0Profile
        from ..models.c1_profile import C1Profile
        from ..models.c2_profile import C2Profile

        d = dict(src_dict)
        kind = PeerHealthEntryKind(d.pop("kind"))

        target_id = d.pop("target_id")

        target_revision = d.pop("target_revision")

        def _parse_read_profile(data: object) -> C0Profile | C1Profile | C2Profile:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_data_profile_type_0 = C0Profile.from_dict(data)

                return componentsschemas_data_profile_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_data_profile_type_1 = C1Profile.from_dict(data)

                return componentsschemas_data_profile_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_data_profile_type_2 = C2Profile.from_dict(data)

            return componentsschemas_data_profile_type_2

        read_profile = _parse_read_profile(d.pop("read_profile"))

        peer_health_entry = cls(
            kind=kind,
            target_id=target_id,
            target_revision=target_revision,
            read_profile=read_profile,
        )

        peer_health_entry.additional_properties = d
        return peer_health_entry

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
