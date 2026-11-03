# v9 program-ledger oracle: test design

## Scope and isolation

This oracle is strictly for the existing schema version 9. It adds no candidate
schema, production code, migration, or version. It snapshots all 34 tables and
synthetic rows from the already-frozen v8 SQLite/PostgreSQL fixtures, including
`platform_metadata` and the audit head, plus all eight existing v9 ledger
tables. The frozen path loads separately frozen, source-derived v9 ledger DDL
and fixture-only v9 rows. It does not call `Database.migrate()` to construct
expected DDL. A separate path loads the v8 fixture and runs the existing
v8-to-v9 migration, then loads the same synthetic rows and compares to those
frozen expectations. No real database, source volume,
credential, export, or network is an input. SQLite uses pytest's disposable
`tmp_path` database.

PostgreSQL is opt-in with `SOVITE_TEST_POSTGRES_URL`. The URL is rejected unless
it names PostgreSQL through an explicit Unix socket under `/tmp/` and a dedicated database
whose name contains `test`, `oracle`, or `fixture`. The connecting role must be
non-superuser and must own a fresh `ledger_v9_oracle_<UUID>` schema. Fixture DDL
is run only with that schema as `search_path`; cleanup drops only that UUID
schema. Only host/port URL options are accepted; the explicit socket host must be under `/tmp/`. Inherited libpq `PG*` environment variables are rejected, and the server must report a Unix-socket connection before DDL. These checks do not prove that a local server is disposable or has no sensitive data; the operator must establish a fresh isolated instance before providing the URL. PostgreSQL results must not be claimed unless that
isolated execution actually occurred. Without the URL, PostgreSQL tests skip
(or fail if `SOVITE_LEDGER_SCHEMA_REQUIRE_PG=1`).

## Frozen expectations and provenance

`tests/fixtures/program_ledger_v9_oracle.json` pins the source revision, source
file SHA-256 values, v8 fixture hashes, v9 fixture file hashes, all 42 table
names, per-backend schema/data digests, and row counts. The v9 SQL contains all
eight existing v9 ledger tables with indexes, constraints and cross-table references; row data is
synthetic and includes a historical work intent, unknown receipt, aggregate
overflow evidence, draft census, legacy link, and a settled known Work
obligation. It also includes a settled collection obligation bound to a
complete synthetic collection session and closed execution epoch. Values and
`synthetic://oracle/...` custody references are invented, not assertions about
real records. Expectations are literals captured from the frozen v8-plus-v9
fixture path; tests do not derive them from current metadata. The separate
migrated path is compared against the same frozen expectations and never used
to produce them.
The frozen fixture was captured from source-derived v9 DDL, independent source review is required; it was not generated from the migration-under-test output. This capture used SQLite 3.53.1 and PostgreSQL 17.11 through the specified local Unix socket as a non-superuser; both frozen and migrated paths passed there. No broader version compatibility is implied. The reflected FK schema is normalized only when it equals the generated fixture namespace; other schemas, constraint names, expressions, targets and actions remain in the snapshot.

Before either schema or data snapshots, the oracle compares the complete table
inventory with the frozen 42-name list and fails on missing or unexpected
application tables. The only excluded name is SQLite's `sqlite_sequence`, the
engine-created bookkeeping table used for `AUTOINCREMENT`; no other SQLite
names are hidden. PostgreSQL catalogs are outside the explicitly inspected
fixture schema, so PostgreSQL has no inventory exclusions.

The helper reflects columns, primary/unique keys, indexes, named foreign keys
and actions, and named CHECK expressions for all 42 tables. It hashes rows in
canonical order as well as reporting per-table row counts. Only references to
the generated PostgreSQL namespace are normalized; names, constraints, targets
and actions are retained. Backend-specific schema hashes are intentional:
SQLite and PostgreSQL expose equivalent DDL differently.

## Tests

1. In separate fresh databases, load the v8 fixture plus frozen v9 DDL, or load the same v8 fixture and run the existing v8-to-v9 migration. Seed the same synthetic v9 rows, then compare each schema hash, data hash, and per-table count with the frozen constants and run the runtime structure check.
2. Assert the settled Work and collection obligation/session/epoch rows are
   present and bound; change a historical audit-event target and assert counts
   remain unchanged while the deterministic data hash changes.
3. Add an unexpected table or remove a historical table and assert inventory
   validation rejects either drift.
4. Rename `ledger_receipt_bounds` without changing its expression and assert
   the schema hash changes, the named constraint is visible as drift, and the
   runtime structure check rejects the altered disposable database.

These are fixture/oracle checks, not proof of production migration safety or
PostgreSQL compatibility unless PostgreSQL is explicitly and safely executed.
