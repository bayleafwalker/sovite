"""Dormant collection-owner tables. This module does not expose execution APIs."""
from sqlalchemy import (BigInteger, CheckConstraint, Column, ForeignKeyConstraint,
                        Index, Integer, JSON, MetaData, String, Table, Text,
                        UniqueConstraint)


def define_collection_schema(metadata: MetaData):
    def ident(name, *, primary_key=False, nullable=False):
        return Column(name, String(96), primary_key=primary_key, nullable=nullable)

    def digest(name, *, nullable=False):
        return Column(name, String(64), nullable=nullable)

    def fk(columns, targets, name):
        return ForeignKeyConstraint(columns, targets, name=name, ondelete="RESTRICT", onupdate="RESTRICT")

    scope = Table("program_budget_scopes", metadata,
        ident("id", primary_key=True), Column("policy_revision", Integer, nullable=False),
        Column("window_start_ms", BigInteger, nullable=False), Column("window_end_ms", BigInteger, nullable=False),
        Column("usd_limit_micro", BigInteger, nullable=False), Column("allowance_unit", String(96), nullable=False),
        Column("allowance_limit_micro", BigInteger, nullable=False), Column("slot_limit", Integer, nullable=False),
        Column("policy_digest", String(64), nullable=False),
        CheckConstraint("policy_revision > 0", name="program_scope_revision_positive"),
        CheckConstraint("window_start_ms >= 0 AND window_end_ms > 0", name="program_scope_window_positive"),
        CheckConstraint("usd_limit_micro >= 0 AND allowance_limit_micro >= 0 AND slot_limit >= 0", name="program_scope_limits_nonnegative"))

    subject = Table("program_budget_subjects", metadata,
        ident("scope_id", primary_key=True), ident("subject_id", primary_key=True),
        Column("purpose", String(24), nullable=False), digest("binding_digest"),
        fk(["scope_id"], ["program_budget_scopes.id"], "program_subject_scope_fk"),
        UniqueConstraint("scope_id", "subject_id", "purpose", name="program_subject_purpose_key"),
        CheckConstraint("purpose IN ('work', 'collection')", name="program_subject_purpose"))

    session = Table("collection_sessions", metadata,
        ident("id", primary_key=True), ident("principal_id"), ident("idempotency_key"),
        ident("scope_id"), ident("workspace_id"), ident("repository_id"),
        Column("schema_version", String(96), nullable=False), Column("purpose", String(32), nullable=False),
        Column("generation", String(80), nullable=False), Column("suite_version", String(96), nullable=False),
        digest("suite_digest"), digest("seed_digest"), digest("base_sha"), digest("paths_digest"), digest("deployment_digest"),
        digest("permit_digest"), digest("immutable_digest"), Column("immutable_payload", JSON, nullable=False),
        Column("deadline_ms", BigInteger, nullable=False), Column("retain_until_ms", BigInteger, nullable=False),
        Column("state", String(32), nullable=False), Column("revision", Integer, nullable=False),
        Column("current_epoch", Integer, nullable=True), Column("cancel_revision", Integer, nullable=True),
        Column("cancel_at_ms", BigInteger, nullable=True),
        fk(["scope_id"], ["program_budget_scopes.id"], "collection_session_scope_fk"),
        UniqueConstraint("principal_id", "idempotency_key", name="collection_session_idempotency"),
        CheckConstraint("purpose IN ('operator_c0_collection')", name="collection_session_purpose"),
        CheckConstraint("state IN ('created', 'admitted', 'active', 'quiescing', 'captured', 'complete', 'cancel_pending', 'cancelled', 'failed', 'stopped_unknown')", name="collection_session_state"),
        CheckConstraint("revision >= 0 AND deadline_ms > 0 AND retain_until_ms > 0", name="collection_session_bounds"),
        CheckConstraint("(cancel_revision IS NULL AND cancel_at_ms IS NULL) OR (cancel_revision IS NOT NULL AND cancel_at_ms IS NOT NULL)", name="collection_session_cancel_pair"),
        CheckConstraint("state <> 'cancel_pending' OR cancel_revision IS NOT NULL", name="collection_session_cancel_pending"),
        CheckConstraint("state <> 'cancelled' OR cancel_revision IS NOT NULL", name="collection_session_cancelled"))

    epoch = Table("collection_epochs", metadata,
        ident("session_id", primary_key=True), Column("epoch", Integer, primary_key=True, nullable=False),
        Column("epoch_purpose", String(24), nullable=False), ident("principal_id"), digest("authorization_digest"),
        Column("deadline_ms", BigInteger, nullable=False), Column("retain_until_ms", BigInteger, nullable=False),
        Column("state", String(24), nullable=False), Column("started_at_ms", BigInteger, nullable=True),
        Column("ended_at_ms", BigInteger, nullable=True),
        fk(["session_id"], ["collection_sessions.id"], "collection_epoch_session_fk"),
        UniqueConstraint("session_id", "epoch", "epoch_purpose", name="collection_epoch_purpose_key"),
        CheckConstraint("epoch > 0 AND deadline_ms > 0 AND retain_until_ms > 0", name="collection_epoch_bounds"),
        CheckConstraint("epoch_purpose IN ('execution', 'reconciliation', 'recovery')", name="collection_epoch_purpose"),
        CheckConstraint("state IN ('authorized', 'active', 'closed', 'unknown')", name="collection_epoch_state"))

    guard = Table("collection_epoch_guards", metadata,
        ident("session_id", primary_key=True), Column("active_epoch", Integer, nullable=True),
        Column("revision", Integer, nullable=False),
        fk(["session_id"], ["collection_sessions.id"], "collection_guard_session_fk"),
        fk(["session_id", "active_epoch"], ["collection_epochs.session_id", "collection_epochs.epoch"], "collection_guard_epoch_fk"),
        CheckConstraint("revision >= 0", name="collection_guard_revision"))

    enrollment = Table("collection_enrollments", metadata,
        ident("id", primary_key=True), ident("session_id"), Column("original_epoch", Integer, nullable=False),
        digest("resource_digest"), digest("boot_digest"), digest("permit_digest"),
        digest("ack_key_digest"), Column("last_ack_sequence", BigInteger, nullable=False),
        Column("retain_until_ms", BigInteger, nullable=False),
        fk(["session_id", "original_epoch"], ["collection_epochs.session_id", "collection_epochs.epoch"], "collection_enrollment_epoch_fk"),
        UniqueConstraint("id", "session_id", name="collection_enrollment_session_key"),
        CheckConstraint("last_ack_sequence >= 0 AND retain_until_ms > 0", name="collection_enrollment_bounds"))

    operation = Table("collection_operations", metadata,
        ident("session_id", primary_key=True), ident("operation_id", primary_key=True),
        Column("epoch", Integer, nullable=False), Column("epoch_purpose", String(24), nullable=False),
        ident("enrollment_id"), Column("ordinal", Integer, nullable=False), ident("idempotency_key"), ident("recipe_id"),
        Column("verb", String(24), nullable=False), digest("session_digest"), digest("request_digest"),
        digest("resource_digest"), digest("boot_digest"), Column("nonce", String(128), nullable=False),
        ident("issuer_key_id"), Column("start_not_after_ms", BigInteger, nullable=False),
        Column("execution_not_after_ms", BigInteger, nullable=False), Column("authorization_claims", Text, nullable=False),
        digest("authorization_claims_digest"), Column("authorization_envelope", Text, nullable=True),
        digest("authorization_envelope_digest", nullable=True), Column("state", String(24), nullable=False),
        Column("audit_sequence", Integer, nullable=False),
        fk(["session_id", "epoch", "epoch_purpose"], ["collection_epochs.session_id", "collection_epochs.epoch", "collection_epochs.epoch_purpose"], "collection_operation_epoch_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "collection_operation_audit_fk"),
        fk(["enrollment_id", "session_id"], ["collection_enrollments.id", "collection_enrollments.session_id"], "collection_operation_enrollment_fk"),
        UniqueConstraint("session_id", "operation_id", "enrollment_id", name="collection_operation_enrollment_key"),
        UniqueConstraint("session_id", "idempotency_key", name="collection_operation_idempotency"),
        UniqueConstraint("session_id", "nonce", name="collection_operation_nonce"),
        UniqueConstraint("session_id", "epoch", "ordinal", name="collection_operation_ordinal"),
        CheckConstraint("epoch_purpose IN ('execution', 'reconciliation', 'recovery')", name="collection_operation_purpose"),
        CheckConstraint("verb IN ('seed-v1', 'exec-v1', 'quiesce-v1', 'export-v1')", name="collection_operation_verb"),
        CheckConstraint("state IN ('intent', 'acknowledged', 'unknown')", name="collection_operation_state"),
        CheckConstraint("epoch_purpose = 'execution' OR verb IN ('quiesce-v1', 'export-v1')", name="collection_operation_recovery_verb"),
        CheckConstraint("ordinal >= 0 AND start_not_after_ms > 0 AND execution_not_after_ms > 0", name="collection_operation_bounds"),
        CheckConstraint("(authorization_envelope IS NULL AND authorization_envelope_digest IS NULL) OR (authorization_envelope IS NOT NULL AND authorization_envelope_digest IS NOT NULL)", name="collection_operation_signed_pair"))

    ack = Table("collection_acknowledgements", metadata,
        ident("enrollment_id", primary_key=True), Column("sequence", BigInteger, primary_key=True, nullable=False),
        ident("session_id"), ident("operation_id"), digest("ack_digest"), ident("key_id"),
        Column("canonical_payload", Text, nullable=False), Column("signature", Text, nullable=False),
        Column("custody_ref", String(512), nullable=False), Column("event", String(24), nullable=False),
        Column("outcome", String(24), nullable=False), Column("audit_sequence", Integer, nullable=False),
        fk(["session_id", "operation_id", "enrollment_id"], ["collection_operations.session_id", "collection_operations.operation_id", "collection_operations.enrollment_id"], "collection_ack_operation_fk"),
        fk(["enrollment_id", "session_id"], ["collection_enrollments.id", "collection_enrollments.session_id"], "collection_ack_enrollment_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "collection_ack_audit_fk"),
        UniqueConstraint("ack_digest", name="collection_ack_digest_unique"),
        UniqueConstraint("enrollment_id", "sequence", "session_id", name="collection_ack_session_key"),
        CheckConstraint("sequence > 0", name="collection_ack_sequence_positive"),
        CheckConstraint("event IN ('STARTED', 'COMPLETED', 'QUIESCED')", name="collection_ack_event"),
        CheckConstraint("outcome IN ('known', 'unknown')", name="collection_ack_outcome"))

    checkpoint = Table("collection_checkpoints", metadata,
        ident("session_id", primary_key=True), Column("sequence", Integer, primary_key=True, nullable=False),
        digest("artifact_digest"), Column("custody_ref", String(512), nullable=False),
        Column("retain_until_ms", BigInteger, nullable=False), Column("audit_sequence", Integer, nullable=False),
        fk(["session_id"], ["collection_sessions.id"], "collection_checkpoint_session_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "collection_checkpoint_audit_fk"),
        CheckConstraint("sequence > 0 AND retain_until_ms > 0", name="collection_checkpoint_bounds"))

    observation = Table("collection_observations", metadata,
        ident("session_id", primary_key=True), Column("sequence", Integer, primary_key=True, nullable=False),
        ident("recipe_id"), Column("recipe_version", String(96), nullable=False), Column("status", String(24), nullable=False),
        digest("payload_digest"), Column("custody_ref", String(512), nullable=False),
        Column("retain_until_ms", BigInteger, nullable=False), Column("audit_sequence", Integer, nullable=False),
        fk(["session_id"], ["collection_sessions.id"], "collection_observation_session_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "collection_observation_audit_fk"),
        UniqueConstraint("session_id", "sequence", "recipe_id", "recipe_version", name="collection_observation_recipe_unique"),
        CheckConstraint("sequence > 0 AND retain_until_ms > 0", name="collection_observation_bounds"),
        CheckConstraint("status IN ('passed', 'failed', 'not_run', 'unknown')", name="collection_observation_status"))

    reservation = Table("program_reservations", metadata,
        ident("scope_id", primary_key=True), ident("call_id", primary_key=True), ident("subject_id"),
        Column("purpose", String(24), nullable=False), ident("effect_key"),
        Column("reserved_usd_micro", BigInteger, nullable=False), Column("reserved_allowance_micro", BigInteger, nullable=False),
        Column("actual_usd_micro", BigInteger, nullable=True), Column("actual_allowance_micro", BigInteger, nullable=True),
        digest("permit_digest"), digest("meter_digest"), digest("price_digest"),
        Column("permit_expires_ms", BigInteger, nullable=False), Column("state", String(24), nullable=False),
        Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "subject_id", "purpose"], ["program_budget_subjects.scope_id", "program_budget_subjects.subject_id", "program_budget_subjects.purpose"], "program_reservation_subject_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "program_reservation_audit_fk"),
        UniqueConstraint("scope_id", "subject_id", "effect_key", name="program_reservation_subject_effect"),
        UniqueConstraint("scope_id", "call_id", "subject_id", name="program_reservation_subject_call"),
        CheckConstraint("purpose IN ('work', 'collection')", name="program_reservation_purpose"),
        CheckConstraint("state IN ('reserved', 'settled', 'unknown')", name="program_reservation_state"),
        CheckConstraint("reserved_usd_micro >= 0 AND reserved_allowance_micro >= 0 AND permit_expires_ms > 0", name="program_reservation_bounds"),
        CheckConstraint("actual_usd_micro IS NULL OR actual_usd_micro >= 0", name="program_reservation_actual_usd"),
        CheckConstraint("actual_allowance_micro IS NULL OR actual_allowance_micro >= 0", name="program_reservation_actual_allowance"))

    slot = Table("program_slots", metadata,
        ident("scope_id", primary_key=True), ident("slot_id", primary_key=True), ident("subject_id"),
        ident("call_id"), Column("state", String(24), nullable=False), Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "subject_id"], ["program_budget_subjects.scope_id", "program_budget_subjects.subject_id"], "program_slot_subject_fk"),
        fk(["scope_id", "call_id", "subject_id"], ["program_reservations.scope_id", "program_reservations.call_id", "program_reservations.subject_id"], "program_slot_reservation_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "program_slot_audit_fk"),
        UniqueConstraint("scope_id", "call_id", name="program_slot_call_unique"),
        CheckConstraint("state IN ('held', 'released', 'unknown')", name="program_slot_state"))

    retention = Table("collection_retention_refs", metadata,
        ident("session_id", primary_key=True), ident("reference_id", primary_key=True),
        Column("kind", String(24), nullable=False), digest("object_digest"),
        Column("custody_ref", String(512), nullable=False), Column("retain_until_ms", BigInteger, nullable=False),
        Column("hold", String(24), nullable=False), Column("audit_sequence", Integer, nullable=False),
        Column("checkpoint_sequence", Integer, nullable=True), Column("observation_sequence", Integer, nullable=True),
        ident("ack_enrollment_id", nullable=True), Column("ack_sequence", BigInteger, nullable=True),
        Column("qualification_id", String(64), nullable=True),
        fk(["session_id", "checkpoint_sequence"], ["collection_checkpoints.session_id", "collection_checkpoints.sequence"], "collection_retention_checkpoint_fk"),
        fk(["session_id", "observation_sequence"], ["collection_observations.session_id", "collection_observations.sequence"], "collection_retention_observation_fk"),
        fk(["ack_enrollment_id", "ack_sequence", "session_id"], ["collection_acknowledgements.enrollment_id", "collection_acknowledgements.sequence", "collection_acknowledgements.session_id"], "collection_retention_ack_fk"),
        fk(["qualification_id"], ["qualifications.id"], "collection_retention_qualification_fk"),
        fk(["session_id"], ["collection_sessions.id"], "collection_retention_session_fk"),
        fk(["audit_sequence"], ["audit_events.sequence"], "collection_retention_audit_fk"),
        CheckConstraint("kind IN ('receipt', 'checkpoint', 'observation', 'qualification', 'resource')", name="collection_retention_kind"),
        CheckConstraint("hold IN ('active', 'released')", name="collection_retention_hold"),
        CheckConstraint("retain_until_ms > 0", name="collection_retention_bounds"),
        CheckConstraint("(ack_enrollment_id IS NULL AND ack_sequence IS NULL) OR (ack_enrollment_id IS NOT NULL AND ack_sequence IS NOT NULL)", name="collection_retention_ack_pair"),
        CheckConstraint("kind <> 'receipt' OR ack_sequence IS NOT NULL", name="collection_retention_receipt_required"),
        CheckConstraint("kind <> 'checkpoint' OR checkpoint_sequence IS NOT NULL", name="collection_retention_checkpoint_required"),
        CheckConstraint("kind <> 'observation' OR observation_sequence IS NOT NULL", name="collection_retention_observation_required"),
        CheckConstraint("kind <> 'qualification' OR qualification_id IS NOT NULL", name="collection_retention_qualification_required"))

    for table, columns in ((session, ("state", "retain_until_ms")), (epoch, ("session_id", "state")),
                           (enrollment, ("session_id", "retain_until_ms")), (operation, ("session_id", "state")), (ack, ("session_id", "operation_id")),
                           (checkpoint, ("retain_until_ms",)), (observation, ("retain_until_ms",)),
                           (reservation, ("scope_id", "state")), (slot, ("scope_id", "state")),
                           (retention, ("hold", "retain_until_ms"))):
        Index("ix_" + table.name + "_owner", *(table.c[name] for name in columns))
    return (scope, subject, session, epoch, guard, enrollment, operation, ack, checkpoint, observation, reservation, slot, retention)
