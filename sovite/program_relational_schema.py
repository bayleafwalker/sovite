"""Dormant additive Unit 4c relational schema; no owner or activation APIs."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.compiler import compiles


class _LowerHexDigestConstraint(CheckConstraint):
    """Lowercase SHA-256 constraint with portable SQLite/PostgreSQL DDL."""

    def __init__(self, *columns: str, nullable: tuple[str, ...] = (), name: str):
        self.digest_columns = tuple(columns)
        self.nullable_digests = frozenset(nullable)
        terms = []
        for column in self.digest_columns:
            valid = f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
            terms.append(f"({column} IS NULL OR ({valid}))" if column in self.nullable_digests else f"({valid})")
        super().__init__(" AND ".join(terms), name=name)


@compiles(_LowerHexDigestConstraint, "postgresql")
def _compile_postgresql_digest(constraint, compiler, **kw):
    terms = []
    for column in constraint.digest_columns:
        quoted = compiler.preparer.quote(column)
        valid = f"length({quoted}) = 64 AND {quoted} ~ '^[0-9a-f]{{64}}$'"
        terms.append(f"({quoted} IS NULL OR ({valid}))" if column in constraint.nullable_digests else f"({valid})")
    name = compiler.preparer.format_constraint(constraint)
    return f"CONSTRAINT {name} CHECK ({' AND '.join(terms)})"


def define_program_relational_schema(metadata: MetaData):
    """Define unpopulated v10 tables. All evidence/projection rows are owner-authored."""

    def text_col(name, length=96, *, nullable=False, primary_key=False):
        return Column(name, String(length), nullable=nullable, primary_key=primary_key)

    def digest_col(name, *, nullable=False, primary_key=False):
        return Column(name, String(64), nullable=nullable, primary_key=primary_key)

    def integer_col(name, *, big=True, nullable=False, primary_key=False):
        return Column(name, BigInteger if big else Integer, nullable=nullable, primary_key=primary_key)

    def fk(columns, targets, name):
        return ForeignKeyConstraint(
            columns,
            targets,
            name=name,
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=False,
        )

    def audit_fk(name):
        return fk(["audit_sequence"], ["audit_events.sequence"], name)

    projection = Table(
        "program_work_projection_bindings", metadata,
        text_col("scope_id", primary_key=True), text_col("call_id", primary_key=True),
        text_col("subject_id"), text_col("purpose", 24), text_col("work_job_id", 64),
        text_col("attempt_id"), text_col("slot_id"), integer_col("policy_revision"),
        digest_col("policy_digest"), integer_col("projection_revision"),
        digest_col("projection_digest"), integer_col("audit_sequence", big=False),
        UniqueConstraint("work_job_id", "call_id", name="v10_projection_job_call_uq"),
        UniqueConstraint("scope_id", "work_job_id", "attempt_id", "call_id", name="v10_projection_attempt_uq"),
        fk(["scope_id", "call_id"], ["program_call_intents.scope_id", "program_call_intents.call_id"], "v10_projection_intent_fk"),
        fk(["scope_id", "call_id", "subject_id"], ["program_reservations.scope_id", "program_reservations.call_id", "program_reservations.subject_id"], "v10_projection_reservation_fk"),
        fk(["scope_id", "slot_id"], ["program_slots.scope_id", "program_slots.slot_id"], "v10_projection_slot_fk"),
        fk(["scope_id", "subject_id", "purpose"], ["program_subject_registry.scope_id", "program_subject_registry.subject_id", "program_subject_registry.purpose"], "v10_projection_registry_fk"),
        fk(["scope_id", "policy_revision", "policy_digest"], ["program_policy_revisions.scope_id", "program_policy_revisions.revision", "program_policy_revisions.policy_digest"], "v10_projection_policy_fk"),
        fk(["work_job_id"], ["jobs.id"], "v10_projection_job_fk"),
        audit_fk("v10_projection_audit_fk"),
        CheckConstraint("purpose = 'work' AND subject_id = work_job_id AND projection_revision > 0 AND audit_sequence >= 1 AND audit_sequence <= 2147483647", name="v10_projection_values_ck"),
        _LowerHexDigestConstraint("policy_digest", "projection_digest", name="v10_projection_digest_ck"),
    )

    manifest = Table(
        "program_census_manifests", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        text_col("census_generation", 32), integer_col("policy_revision"), digest_col("policy_digest"),
        text_col("snapshot_id"), digest_col("snapshot_digest"), digest_col("fence_digest"),
        integer_col("owner_epoch"), text_col("perimeter_version", 32), digest_col("perimeter_digest"),
        text_col("stream_set_version", 32), digest_col("required_stream_digest"),
        text_col("manifest_version", 32), integer_col("created_audit_sequence", big=False),
        fk(
            ["scope_id", "batch_id", "census_generation", "policy_revision", "policy_digest", "snapshot_digest", "fence_digest", "owner_epoch"],
            ["program_census_batches.scope_id", "program_census_batches.batch_id", "program_census_batches.census_generation", "program_census_batches.policy_revision", "program_census_batches.policy_digest", "program_census_batches.snapshot_digest", "program_census_batches.fence_digest", "program_census_batches.owner_epoch"],
            "v10_manifest_batch_fk",
        ),
        fk(["scope_id", "policy_revision", "policy_digest"], ["program_policy_revisions.scope_id", "program_policy_revisions.revision", "program_policy_revisions.policy_digest"], "v10_manifest_policy_fk"),
        fk(["created_audit_sequence"], ["audit_events.sequence"], "v10_manifest_audit_fk"),
        CheckConstraint("owner_epoch >= 0 AND created_audit_sequence >= 1 AND created_audit_sequence <= 2147483647", name="v10_manifest_values_ck"),
        _LowerHexDigestConstraint("policy_digest", "snapshot_digest", "fence_digest", "perimeter_digest", "required_stream_digest", name="v10_manifest_digest_ck"),
    )

    perimeter = Table(
        "program_census_perimeter_members", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        text_col("member_kind", 24, primary_key=True), digest_col("member_digest", primary_key=True),
        text_col("relationship", 24), digest_col("evidence_digest"), integer_col("audit_sequence", big=False),
        fk(["scope_id", "batch_id"], ["program_census_manifests.scope_id", "program_census_manifests.batch_id"], "v10_perimeter_manifest_fk"),
        audit_fk("v10_perimeter_audit_fk"),
        CheckConstraint("member_kind IN ('provider_account','credential_principal','execution_route','target_boundary','workload_principal','logical_scope') AND relationship IN ('included','shared','excluded_proven') AND audit_sequence >= 1 AND audit_sequence <= 2147483647", name="v10_perimeter_values_ck"),
        _LowerHexDigestConstraint("member_digest", "evidence_digest", name="v10_perimeter_digest_ck"),
    )

    manifest_state = Table(
        "program_census_manifest_states", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        integer_col("state_revision", primary_key=True), text_col("state", 16),
        integer_col("required_stream_count"), integer_col("complete_stream_count"),
        integer_col("source_row_count"), integer_col("union_row_count"), integer_col("unmatched_count"),
        integer_col("union_usd_micro"), integer_col("union_allowance_micro"), integer_col("held_slots"),
        digest_col("reconciliation_digest", nullable=True), integer_col("audit_sequence", big=False),
        UniqueConstraint("scope_id", "batch_id", "audit_sequence", name="v10_manifest_state_audit_uq"),
        fk(["scope_id", "batch_id"], ["program_census_manifests.scope_id", "program_census_manifests.batch_id"], "v10_manifest_state_manifest_fk"),
        audit_fk("v10_manifest_state_audit_fk"),
        CheckConstraint(
            "state_revision > 0 AND state IN ('draft','scanning','complete','aborted') AND "
            "required_stream_count >= 0 AND complete_stream_count >= 0 AND source_row_count >= 0 AND "
            "union_row_count >= 0 AND unmatched_count >= 0 AND union_usd_micro >= 0 AND "
            "union_allowance_micro >= 0 AND held_slots >= 0 AND audit_sequence >= 1 AND audit_sequence <= 2147483647 AND "
            "((state IN ('draft','scanning','aborted') AND reconciliation_digest IS NULL) OR "
            "(state = 'complete' AND reconciliation_digest IS NOT NULL AND unmatched_count = 0 AND required_stream_count = complete_stream_count))",
            name="v10_manifest_state_values_ck",
        ),
        _LowerHexDigestConstraint("reconciliation_digest", nullable=("reconciliation_digest",), name="v10_manifest_state_digest_ck"),
    )

    streams = Table(
        "program_census_streams", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        text_col("stream_id", primary_key=True), text_col("stream_kind", 32),
        text_col("source_relation"), text_col("source_schema_version", 32), text_col("key_codec", 32),
        text_col("canonicalization_version", 32), text_col("snapshot_id"),
        Column("required", SmallInteger, nullable=False), integer_col("audit_sequence", big=False),
        UniqueConstraint("scope_id", "batch_id", "stream_kind", name="v10_stream_kind_uq"),
        fk(["scope_id", "batch_id"], ["program_census_manifests.scope_id", "program_census_manifests.batch_id"], "v10_stream_manifest_fk"),
        audit_fk("v10_stream_audit_fk"),
        CheckConstraint("required IN (0,1) AND stream_kind IN ('work_jobs','work_leases','canonical_ledger','collection_sessions','collection_state','audit','cross_scope') AND audit_sequence >= 1 AND audit_sequence <= 2147483647", name="v10_stream_values_ck"),
    )

    stream_state = Table(
        "program_census_stream_states", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        text_col("stream_id", primary_key=True), integer_col("state_revision", primary_key=True),
        text_col("state", 16), integer_col("row_count"), integer_col("page_count"), integer_col("byte_count"),
        digest_col("stream_digest", nullable=True), integer_col("last_page_number", nullable=True),
        integer_col("audit_sequence", big=False),
        fk(["scope_id", "batch_id", "stream_id"], ["program_census_streams.scope_id", "program_census_streams.batch_id", "program_census_streams.stream_id"], "v10_stream_state_stream_fk"),
        audit_fk("v10_stream_state_audit_fk"),
        CheckConstraint(
            "state_revision > 0 AND state IN ('scanning','complete','aborted') AND row_count >= 0 AND page_count >= 0 AND byte_count >= 0 AND audit_sequence >= 1 AND audit_sequence <= 2147483647 AND "
            "((state IN ('scanning','aborted') AND stream_digest IS NULL AND last_page_number IS NULL) OR "
            "(state = 'complete' AND stream_digest IS NOT NULL AND last_page_number IS NOT NULL AND "
            "((row_count = 0 AND page_count = 0 AND byte_count = 0 AND last_page_number = 0) OR "
            "(row_count > 0 AND page_count > 0 AND byte_count > 0 AND last_page_number = page_count))))",
            name="v10_stream_state_values_ck",
        ),
        _LowerHexDigestConstraint("stream_digest", nullable=("stream_digest",), name="v10_stream_state_digest_ck"),
    )

    pages = Table(
        "program_census_pages", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        text_col("stream_id", primary_key=True), integer_col("page_number", primary_key=True),
        Column("first_key", LargeBinary, nullable=False), Column("last_key", LargeBinary, nullable=False),
        Column("cursor_before", LargeBinary, nullable=True), Column("cursor_after", LargeBinary, nullable=False),
        integer_col("row_count"), integer_col("byte_count"), digest_col("page_digest"),
        digest_col("previous_page_digest", nullable=True), digest_col("chain_digest"),
        text_col("canonicalization_version", 32), integer_col("audit_sequence", big=False),
        UniqueConstraint("scope_id", "batch_id", "stream_id", "cursor_after", name="v10_page_cursor_uq"),
        fk(["scope_id", "batch_id", "stream_id"], ["program_census_streams.scope_id", "program_census_streams.batch_id", "program_census_streams.stream_id"], "v10_page_stream_fk"),
        audit_fk("v10_page_audit_fk"),
        CheckConstraint(
            "page_number > 0 AND row_count > 0 AND byte_count > 0 AND "
            "length(first_key) >= 1 AND length(first_key) <= 512 AND length(last_key) >= 1 AND length(last_key) <= 512 AND "
            "length(cursor_after) >= 1 AND length(cursor_after) <= 512 AND "
            "(cursor_before IS NULL OR (length(cursor_before) >= 1 AND length(cursor_before) <= 512)) AND "
            "audit_sequence >= 1 AND audit_sequence <= 2147483647 AND "
            "((page_number = 1 AND cursor_before IS NULL AND previous_page_digest IS NULL) OR "
            "(page_number > 1 AND cursor_before IS NOT NULL AND previous_page_digest IS NOT NULL))",
            name="v10_page_values_ck",
        ),
        _LowerHexDigestConstraint("page_digest", "chain_digest", "previous_page_digest", nullable=("previous_page_digest",), name="v10_page_digest_ck"),
    )

    confirmations = Table(
        "program_census_confirmations", metadata,
        text_col("scope_id", primary_key=True), text_col("batch_id", primary_key=True),
        text_col("confirmation_id", primary_key=True), text_col("subject_kind", 24),
        text_col("subject_id"), text_col("object_ref", 512), digest_col("object_digest"),
        integer_col("byte_count"), text_col("representation_version", 32),
        digest_col("verifier_identity_digest"), text_col("result", 16), integer_col("observed_at_ms"),
        integer_col("audit_sequence", big=False),
        UniqueConstraint("scope_id", "batch_id", "subject_kind", "subject_id", "verifier_identity_digest", "object_digest", name="v10_confirmation_identity_uq"),
        fk(["scope_id", "batch_id"], ["program_census_manifests.scope_id", "program_census_manifests.batch_id"], "v10_confirmation_manifest_fk"),
        audit_fk("v10_confirmation_audit_fk"),
        CheckConstraint("subject_kind IN ('manifest','perimeter_member','stream','page','source_snapshot') AND result IN ('confirmed','rejected') AND byte_count >= 1 AND byte_count <= 1048576 AND observed_at_ms >= 0 AND audit_sequence >= 1 AND audit_sequence <= 2147483647", name="v10_confirmation_values_ck"),
        _LowerHexDigestConstraint("object_digest", "verifier_identity_digest", name="v10_confirmation_digest_ck"),
    )

    gate_bindings = Table(
        "program_census_gate_bindings", metadata,
        text_col("scope_id", primary_key=True), integer_col("gate_revision", primary_key=True),
        text_col("batch_id"), text_col("census_generation", 32), integer_col("manifest_revision"),
        digest_col("snapshot_digest"), digest_col("fence_digest"), integer_col("owner_epoch"),
        digest_col("perimeter_digest"), digest_col("reconciliation_digest"), integer_col("audit_sequence", big=False),
        UniqueConstraint("scope_id", "batch_id", "manifest_revision", name="v10_gate_manifest_uq"),
        fk(["scope_id"], ["program_gate_state.scope_id"], "v10_gate_scope_fk"),
        fk(["scope_id", "batch_id"], ["program_census_manifests.scope_id", "program_census_manifests.batch_id"], "v10_gate_manifest_fk"),
        fk(["scope_id", "batch_id", "manifest_revision"], ["program_census_manifest_states.scope_id", "program_census_manifest_states.batch_id", "program_census_manifest_states.state_revision"], "v10_gate_state_fk"),
        audit_fk("v10_gate_audit_fk"),
        CheckConstraint("gate_revision > 0 AND manifest_revision > 0 AND owner_epoch >= 0 AND audit_sequence >= 1 AND audit_sequence <= 2147483647", name="v10_gate_values_ck"),
        _LowerHexDigestConstraint("snapshot_digest", "fence_digest", "perimeter_digest", "reconciliation_digest", name="v10_gate_digest_ck"),
    )

    Index("v10_projection_scope_job_idx", projection.c.scope_id, projection.c.work_job_id)
    Index("v10_manifest_generation_idx", manifest.c.scope_id, manifest.c.census_generation)
    Index("v10_stream_required_idx", streams.c.scope_id, streams.c.batch_id, streams.c.required, streams.c.stream_id)
    Index("v10_confirmation_subject_idx", confirmations.c.scope_id, confirmations.c.batch_id, confirmations.c.subject_kind, confirmations.c.subject_id)
    Index("v10_gate_batch_idx", gate_bindings.c.scope_id, gate_bindings.c.batch_id, gate_bindings.c.gate_revision)

    return projection, manifest, perimeter, manifest_state, streams, stream_state, pages, confirmations, gate_bindings
