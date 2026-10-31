from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..models.target_catalog_schema_version import TargetCatalogSchemaVersion

if TYPE_CHECKING:
    from ..models.lab_entry import LabEntry
    from ..models.peer_health_entry import PeerHealthEntry


T = TypeVar("T", bound="TargetCatalog")


@_attrs_define
class TargetCatalog:
    """
    Attributes:
        schema_version (TargetCatalogSchemaVersion):
        entries (list[LabEntry | PeerHealthEntry]):
    """

    schema_version: TargetCatalogSchemaVersion
    entries: list[LabEntry | PeerHealthEntry]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.peer_health_entry import PeerHealthEntry

        schema_version = self.schema_version.value

        entries = []
        for entries_item_data in self.entries:
            entries_item: dict[str, Any]
            if isinstance(entries_item_data, PeerHealthEntry):
                entries_item = entries_item_data.to_dict()
            else:
                entries_item = entries_item_data.to_dict()

            entries.append(entries_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema_version": schema_version,
                "entries": entries,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.lab_entry import LabEntry
        from ..models.peer_health_entry import PeerHealthEntry

        d = dict(src_dict)
        schema_version = TargetCatalogSchemaVersion(d.pop("schema_version"))

        entries = []
        _entries = d.pop("entries")
        for entries_item_data in _entries:

            def _parse_entries_item(data: object) -> LabEntry | PeerHealthEntry:
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

            entries_item = _parse_entries_item(entries_item_data)

            entries.append(entries_item)

        target_catalog = cls(
            schema_version=schema_version,
            entries=entries,
        )

        target_catalog.additional_properties = d
        return target_catalog

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
