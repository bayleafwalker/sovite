"""Portable deterministic snapshots for the frozen, synthetic v8/v9 oracle."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import MetaData, Table, inspect, select


HISTORICAL_TABLES = (
    "audit_events",
    "auth_sessions",
    "binding_runtime_state",
    "bundle_revisions",
    "collection_acknowledgements",
    "collection_checkpoints",
    "collection_enrollments",
    "collection_epoch_guards",
    "collection_epochs",
    "collection_observations",
    "collection_operations",
    "collection_retention_refs",
    "collection_sessions",
    "execution_authority",
    "grant_runtime_state",
    "helper_enrollments",
    "helper_sessions",
    "inference_requests",
    "jobs",
    "platform_metadata",
    "program_budget_scopes",
    "program_budget_subjects",
    "program_reservations",
    "program_slots",
    "qualifications",
    "resource_gateway_enrollments",
    "resource_output_grants",
    "resource_owner_requests",
    "resource_workload_scopes",
    "setup_drafts",
    "setup_principals",
    "worker_execution_leases",
    "worker_execution_operations",
    "worker_heartbeats",
)

LEDGER_TABLES = (
    "program_policy_revisions",
    "program_census_batches",
    "program_gate_state",
    "program_subject_registry",
    "program_call_intents",
    "program_overflow_evidence",
    "program_call_receipts",
    "program_legacy_links",
)

ORACLE_TABLES = HISTORICAL_TABLES + LEDGER_TABLES

# SQLite may create sqlite_sequence as implementation bookkeeping for tables
# declared with AUTOINCREMENT. It is not an application table and is excluded
# explicitly; ordinary SQLite tables (including any other sqlite_* name) are
# still inventoried. PostgreSQL's catalogs live in pg_catalog, outside the
# explicitly selected fixture schema, so no PostgreSQL table names are hidden.
DIALECT_INTERNAL_TABLES = {
    "sqlite": frozenset({"sqlite_sequence"}),
}


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def assert_table_inventory(connection, schema=None):
    """Reject missing or unexpected application tables before snapshotting."""
    inspector = inspect(connection)
    internal = DIALECT_INTERNAL_TABLES.get(connection.dialect.name, frozenset())
    actual = set(inspector.get_table_names(schema=schema)) - internal
    expected = set(ORACLE_TABLES)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise AssertionError(
            "oracle table inventory mismatch: "
            f"missing={missing}; unexpected={unexpected}"
        )


def schema_snapshot(connection, schema=None):
    """Hash reflected structure, including named constraints and FK actions."""
    assert_table_inventory(connection, schema)
    inspector = inspect(connection)
    snapshot = []
    for name in ORACLE_TABLES:
        columns = inspector.get_columns(name, schema=schema)
        pk = inspector.get_pk_constraint(name, schema=schema)
        uniques = inspector.get_unique_constraints(name, schema=schema)
        indexes = inspector.get_indexes(name, schema=schema)
        foreign_keys = inspector.get_foreign_keys(
            name, schema=schema,
            **({"postgresql_ignore_search_path": True}
               if connection.dialect.name == "postgresql" else {}),
        )
        checks = inspector.get_check_constraints(name, schema=schema)
        snapshot.append({
            "table": name,
            "columns": [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": bool(column["nullable"]),
                    "default": column.get("default"),
                }
                for column in columns
            ],
            "primary_key": {
                "name": pk.get("name"),
                "columns": pk.get("constrained_columns") or [],
            },
            "unique_constraints": sorted(
                ({"name": row.get("name"), "columns": row.get("column_names") or []}
                 for row in uniques),
                key=lambda row: (row["name"] or "", row["columns"]),
            ),
            "indexes": sorted(
                ({"name": row.get("name"), "columns": row.get("column_names") or [],
                  "unique": bool(row.get("unique"))}
                 for row in indexes),
                key=lambda row: (row["name"] or "", row["columns"]),
            ),
            "foreign_keys": sorted(
                ({"name": row.get("name"), "columns": row.get("constrained_columns") or [],
                  "schema": ("__ORACLE_SCHEMA__" if schema is not None and row.get("referred_schema") == schema
                             else row.get("referred_schema")),
                  "table": row.get("referred_table"),
                  "referred_columns": row.get("referred_columns") or [],
                  "options": row.get("options") or {}}
                 for row in foreign_keys),
                key=lambda row: (row["name"] or "", row["columns"], row["table"] or ""),
            ),
            "checks": sorted(
                ({"name": row.get("name"), "sqltext": row.get("sqltext"),
                  "dialect_options": row.get("dialect_options") or {}}
                 for row in checks),
                key=lambda row: row["name"] or "",
            ),
        })
    payload = canonical_json(snapshot)
    return sha256(payload), snapshot


def data_snapshot(connection, schema=None):
    """Hash rows in stable table/column order; return row counts separately."""
    assert_table_inventory(connection, schema)
    metadata = MetaData()
    snapshot = []
    counts = {}
    for name in ORACLE_TABLES:
        table = Table(name, metadata, schema=schema, autoload_with=connection)
        rows = [list(row) for row in connection.execute(select(table))]
        rows.sort(key=canonical_json)
        snapshot.append({"table": name, "rows": rows})
        counts[name] = len(rows)
    payload = canonical_json(snapshot)
    return sha256(payload), counts
