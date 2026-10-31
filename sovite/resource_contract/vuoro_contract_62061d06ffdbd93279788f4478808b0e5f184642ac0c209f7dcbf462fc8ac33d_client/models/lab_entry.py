from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.lab_entry_kind import LabEntryKind

if TYPE_CHECKING:
    from ..models.c0_profile import C0Profile
    from ..models.c1_profile import C1Profile
    from ..models.c2_profile import C2Profile


T = TypeVar("T", bound="LabEntry")


@_attrs_define
class LabEntry:
    """
    Attributes:
        kind (LabEntryKind):
        target_id (str):
        target_revision (str):
        create_profile (C0Profile | C1Profile | C2Profile):
        observe_profile (C0Profile | C1Profile | C2Profile):
        stop_profile (C0Profile | C1Profile | C2Profile):
    """

    kind: LabEntryKind
    target_id: str
    target_revision: str
    create_profile: C0Profile | C1Profile | C2Profile
    observe_profile: C0Profile | C1Profile | C2Profile
    stop_profile: C0Profile | C1Profile | C2Profile
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.c0_profile import C0Profile
        from ..models.c1_profile import C1Profile

        kind = self.kind.value

        target_id = self.target_id

        target_revision = self.target_revision

        create_profile: dict[str, Any]
        if isinstance(self.create_profile, C0Profile) or isinstance(
            self.create_profile, C1Profile
        ):
            create_profile = self.create_profile.to_dict()
        else:
            create_profile = self.create_profile.to_dict()

        observe_profile: dict[str, Any]
        if isinstance(self.observe_profile, C0Profile) or isinstance(
            self.observe_profile, C1Profile
        ):
            observe_profile = self.observe_profile.to_dict()
        else:
            observe_profile = self.observe_profile.to_dict()

        stop_profile: dict[str, Any]
        if isinstance(self.stop_profile, C0Profile) or isinstance(
            self.stop_profile, C1Profile
        ):
            stop_profile = self.stop_profile.to_dict()
        else:
            stop_profile = self.stop_profile.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "target_id": target_id,
                "target_revision": target_revision,
                "create_profile": create_profile,
                "observe_profile": observe_profile,
                "stop_profile": stop_profile,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.c0_profile import C0Profile
        from ..models.c1_profile import C1Profile
        from ..models.c2_profile import C2Profile

        d = dict(src_dict)
        kind = LabEntryKind(d.pop("kind"))

        target_id = d.pop("target_id")

        target_revision = d.pop("target_revision")

        def _parse_create_profile(data: object) -> C0Profile | C1Profile | C2Profile:
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

        create_profile = _parse_create_profile(d.pop("create_profile"))

        def _parse_observe_profile(data: object) -> C0Profile | C1Profile | C2Profile:
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

        observe_profile = _parse_observe_profile(d.pop("observe_profile"))

        def _parse_stop_profile(data: object) -> C0Profile | C1Profile | C2Profile:
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

        stop_profile = _parse_stop_profile(d.pop("stop_profile"))

        lab_entry = cls(
            kind=kind,
            target_id=target_id,
            target_revision=target_revision,
            create_profile=create_profile,
            observe_profile=observe_profile,
            stop_profile=stop_profile,
        )

        lab_entry.additional_properties = d
        return lab_entry

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
