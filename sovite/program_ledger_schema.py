"""Proposed dormant program-ledger tables; no provisioning or activation APIs.

The tables are created only by the offline migration. This module supplies no
provisioning, verifier, cutover, or effect authority.
"""
from sqlalchemy import (BigInteger, CheckConstraint, Column, ForeignKeyConstraint,
                        Index, Integer, MetaData, String, Table, Text, UniqueConstraint)


def define_program_ledger_schema(metadata: MetaData):
    def ident(name, *, pk=False, nullable=False):
        return Column(name, String(96), primary_key=pk, nullable=nullable)

    def digest(name, *, nullable=False):
        return Column(name, String(64), nullable=nullable)

    def amount(name, *, nullable=False):
        return Column(name, BigInteger, nullable=nullable)

    def fk(cols, targets, name):
        return ForeignKeyConstraint(cols, targets, name=name, ondelete="RESTRICT", onupdate="RESTRICT")

    def audit(name):
        return fk(["audit_sequence"], ["audit_events.sequence"], name)

    policy = Table("program_policy_revisions", metadata,
        ident("scope_id", pk=True), Column("revision", BigInteger, primary_key=True),
        digest("policy_digest"), digest("previous_digest", nullable=True), Column("policy_canonical", Text, nullable=False),
        Column("currency", String(3), nullable=False), ident("allowance_unit"),
        amount("window_start_ms"), amount("window_end_ms"), amount("usd_limit_micro"),
        amount("allowance_limit_micro"), amount("slot_limit"), digest("price_policy_digest"),
        digest("meter_policy_digest"), digest("permit_policy_digest"), digest("manifest_digest"), digest("source_identity_digest"),
        Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id"], ["program_budget_scopes.id"], "ledger_policy_scope_fk"), audit("ledger_policy_audit_fk"),
        UniqueConstraint("scope_id", "policy_digest", name="ledger_policy_digest_key"),
        UniqueConstraint("scope_id", "revision", "policy_digest", name="ledger_policy_binding_key"),
        CheckConstraint("revision > 0 AND window_start_ms >= 0 AND window_end_ms > window_start_ms", name="ledger_policy_window"),
        CheckConstraint("usd_limit_micro >= 0 AND allowance_limit_micro >= 0 AND slot_limit >= 0", name="ledger_policy_limits"),
        CheckConstraint("currency = 'USD'", name="ledger_policy_currency"))

    batch = Table("program_census_batches", metadata,
        ident("scope_id", pk=True), ident("batch_id", pk=True), amount("policy_revision"), digest("policy_digest"),
        Column("census_generation", String(32), nullable=False), digest("snapshot_digest", nullable=True),
        digest("fence_digest", nullable=True), Column("owner_epoch", BigInteger, nullable=True),
        Column("state", String(24), nullable=False),
        amount("source_count"), amount("mapped_count"), amount("unmatched_count"),
        amount("reserved_usd_micro"), amount("settled_usd_micro"), amount("unknown_usd_micro"),
        amount("reserved_allowance_micro"), amount("settled_allowance_micro"), amount("unknown_allowance_micro"),
        amount("held_slots"), amount("unknown_slots"), Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "policy_revision", "policy_digest"], ["program_policy_revisions.scope_id", "program_policy_revisions.revision", "program_policy_revisions.policy_digest"], "ledger_batch_policy_fk"),
        audit("ledger_batch_audit_fk"),
        CheckConstraint("state IN ('draft', 'complete', 'aborted')", name="ledger_batch_state"),
        UniqueConstraint("scope_id", "census_generation", name="ledger_batch_generation_key"),
        UniqueConstraint("scope_id", "batch_id", "census_generation", "policy_revision", "policy_digest", "snapshot_digest", "fence_digest", "owner_epoch", name="ledger_batch_cutover_binding_key"),
        CheckConstraint("source_count >= 0 AND mapped_count >= 0 AND unmatched_count >= 0", name="ledger_batch_counts"),
        CheckConstraint("reserved_usd_micro >= 0 AND settled_usd_micro >= 0 AND unknown_usd_micro >= 0 AND reserved_allowance_micro >= 0 AND settled_allowance_micro >= 0 AND unknown_allowance_micro >= 0 AND held_slots >= 0 AND unknown_slots >= 0", name="ledger_batch_totals"),
        CheckConstraint("state <> 'complete' OR (snapshot_digest IS NOT NULL AND fence_digest IS NOT NULL AND owner_epoch IS NOT NULL AND owner_epoch >= 0)", name="ledger_batch_complete_binding"))

    gate = Table("program_gate_state", metadata,
        ident("scope_id", pk=True), amount("policy_revision"), digest("policy_digest"),
        Column("mode", String(24), nullable=False), amount("revision"), ident("cutover_batch_id", nullable=True),
        digest("cutover_digest", nullable=True), Column("cutover_generation", String(32), nullable=True),
        digest("cutover_fence_digest", nullable=True), Column("cutover_owner_epoch", BigInteger, nullable=True),
        Column("reason_code", String(96), nullable=True),
        Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "policy_revision", "policy_digest"], ["program_policy_revisions.scope_id", "program_policy_revisions.revision", "program_policy_revisions.policy_digest"], "ledger_gate_policy_fk"),
        fk(["scope_id", "cutover_batch_id", "cutover_generation", "policy_revision", "policy_digest", "cutover_digest", "cutover_fence_digest", "cutover_owner_epoch"],
           ["program_census_batches.scope_id", "program_census_batches.batch_id", "program_census_batches.census_generation", "program_census_batches.policy_revision", "program_census_batches.policy_digest", "program_census_batches.snapshot_digest", "program_census_batches.fence_digest", "program_census_batches.owner_epoch"], "ledger_gate_batch_fk"),
        audit("ledger_gate_audit_fk"),
        CheckConstraint("mode IN ('disabled', 'census', 'active', 'inhibited') AND revision >= 0", name="ledger_gate_mode"),
        CheckConstraint("(cutover_batch_id IS NULL AND cutover_digest IS NULL AND cutover_generation IS NULL AND cutover_fence_digest IS NULL AND cutover_owner_epoch IS NULL) OR (cutover_batch_id IS NOT NULL AND cutover_digest IS NOT NULL AND cutover_generation IS NOT NULL AND cutover_fence_digest IS NOT NULL AND cutover_owner_epoch IS NOT NULL AND cutover_owner_epoch >= 0)", name="ledger_gate_cutover_pair"),
        CheckConstraint("mode <> 'active' OR cutover_batch_id IS NOT NULL", name="ledger_gate_active_batch"))

    registry = Table("program_subject_registry", metadata,
        Column("purpose", String(24), primary_key=True), ident("subject_id", pk=True), ident("scope_id"),
        digest("binding_digest"), Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "subject_id", "purpose"], ["program_budget_subjects.scope_id", "program_budget_subjects.subject_id", "program_budget_subjects.purpose"], "ledger_registry_subject_fk"),
        audit("ledger_registry_audit_fk"), UniqueConstraint("subject_id", name="ledger_registry_global_subject_key"),
        UniqueConstraint("scope_id", "subject_id", "purpose", name="ledger_registry_binding_key"),
        CheckConstraint("purpose IN ('work', 'collection')", name="ledger_registry_purpose"))

    intent = Table("program_call_intents", metadata,
        ident("scope_id", pk=True), ident("call_id", pk=True), ident("subject_id"), Column("purpose", String(24), nullable=False),
        ident("effect_key"), ident("idempotency_key"), digest("request_digest"), amount("policy_revision"), digest("policy_digest"),
        digest("price_digest"), digest("meter_digest"), digest("permit_digest"), ident("source_code"), ident("issuer_code"),
        digest("source_identity_digest"), amount("observed_ms"), amount("valid_until_ms"),
        Column("state", String(24), nullable=False), Column("work_job_id", String(64), nullable=True),
        ident("work_attempt_id", nullable=True), digest("work_lease_digest", nullable=True),
        ident("collection_session_id", nullable=True), Column("collection_epoch", Integer, nullable=True),
        digest("collection_context_digest", nullable=True), Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "call_id", "subject_id"], ["program_reservations.scope_id", "program_reservations.call_id", "program_reservations.subject_id"], "ledger_intent_reservation_fk"),
        fk(["scope_id", "subject_id", "purpose"], ["program_subject_registry.scope_id", "program_subject_registry.subject_id", "program_subject_registry.purpose"], "ledger_intent_registry_fk"),
        fk(["scope_id", "policy_revision", "policy_digest"], ["program_policy_revisions.scope_id", "program_policy_revisions.revision", "program_policy_revisions.policy_digest"], "ledger_intent_policy_fk"),
        fk(["work_job_id"], ["jobs.id"], "ledger_intent_job_fk"),
        fk(["collection_session_id", "collection_epoch"], ["collection_epochs.session_id", "collection_epochs.epoch"], "ledger_intent_epoch_fk"),
        audit("ledger_intent_audit_fk"),
        UniqueConstraint("scope_id", "purpose", "subject_id", "idempotency_key", name="ledger_intent_idempotency_key"),
        UniqueConstraint("scope_id", "subject_id", "effect_key", name="ledger_intent_effect_key"),
        CheckConstraint("state IN ('committed', 'known', 'unknown')", name="ledger_intent_state"),
        CheckConstraint("observed_ms >= 0 AND valid_until_ms > observed_ms", name="ledger_intent_time"),
        CheckConstraint("(purpose = 'work' AND work_job_id IS NOT NULL AND work_attempt_id IS NOT NULL AND work_lease_digest IS NOT NULL AND subject_id = work_job_id AND collection_session_id IS NULL AND collection_epoch IS NULL AND collection_context_digest IS NULL) OR (purpose = 'collection' AND work_job_id IS NULL AND work_attempt_id IS NULL AND work_lease_digest IS NULL AND collection_session_id IS NOT NULL AND collection_epoch IS NOT NULL AND collection_epoch > 0 AND collection_context_digest IS NOT NULL AND subject_id = collection_session_id)", name="ledger_intent_context"))

    evidence = Table("program_overflow_evidence", metadata,
        ident("scope_id", pk=True), ident("evidence_id", pk=True), Column("kind", String(24), nullable=False),
        ident("call_id", nullable=True), digest("receipt_digest", nullable=True),
        Column("policy_revision", BigInteger, nullable=True), digest("policy_digest", nullable=True),
        digest("snapshot_digest", nullable=True), Column("usd_decimal", String(128), nullable=False),
        Column("allowance_decimal", String(128), nullable=False), Column("currency", String(3), nullable=False),
        ident("allowance_unit"), digest("source_digest"), Column("custody_ref", String(512), nullable=False),
        amount("custody_bytes"), amount("observed_ms"), Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id"], ["program_budget_scopes.id"], "ledger_evidence_scope_fk"),
        fk(["scope_id", "call_id"], ["program_call_intents.scope_id", "program_call_intents.call_id"], "ledger_evidence_intent_fk"),
        fk(["scope_id", "policy_revision", "policy_digest"], ["program_policy_revisions.scope_id", "program_policy_revisions.revision", "program_policy_revisions.policy_digest"], "ledger_evidence_policy_fk"),
        audit("ledger_evidence_audit_fk"),
        UniqueConstraint("scope_id", "call_id", "receipt_digest", "evidence_id", name="ledger_evidence_receipt_binding"),
        UniqueConstraint("scope_id", "call_id", "receipt_digest", name="ledger_evidence_receipt_unique"),
        UniqueConstraint("scope_id", "policy_revision", "policy_digest", "snapshot_digest", name="ledger_evidence_snapshot_unique"),
        CheckConstraint("(kind = 'call' AND call_id IS NOT NULL AND receipt_digest IS NOT NULL AND policy_revision IS NULL AND policy_digest IS NULL AND snapshot_digest IS NULL) OR (kind = 'aggregate' AND call_id IS NULL AND receipt_digest IS NULL AND policy_revision IS NOT NULL AND policy_digest IS NOT NULL AND snapshot_digest IS NOT NULL)", name="ledger_evidence_branch"),
        CheckConstraint("currency = 'USD' AND custody_bytes > 0 AND custody_bytes <= 1048576 AND observed_ms >= 0", name="ledger_evidence_bounds"))

    receipt = Table("program_call_receipts", metadata,
        ident("scope_id", pk=True), ident("call_id", pk=True), ident("receipt_id", pk=True), digest("receipt_digest"),
        Column("outcome", String(24), nullable=False), amount("actual_usd_micro", nullable=True),
        amount("actual_allowance_micro", nullable=True), ident("evidence_id", nullable=True),
        Column("currency", String(3), nullable=False), ident("allowance_unit"), ident("source_code"),
        ident("issuer_code"), amount("observed_ms"), Column("custody_ref", String(512), nullable=False),
        digest("source_digest"), digest("source_identity_digest"), amount("custody_bytes"), Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "call_id"], ["program_call_intents.scope_id", "program_call_intents.call_id"], "ledger_receipt_intent_fk"),
        fk(["scope_id", "call_id", "receipt_digest", "evidence_id"], ["program_overflow_evidence.scope_id", "program_overflow_evidence.call_id", "program_overflow_evidence.receipt_digest", "program_overflow_evidence.evidence_id"], "ledger_receipt_evidence_fk"),
        audit("ledger_receipt_audit_fk"), UniqueConstraint("receipt_digest", name="ledger_receipt_digest_key"),
        CheckConstraint("(outcome = 'known' AND actual_usd_micro IS NOT NULL AND actual_allowance_micro IS NOT NULL AND evidence_id IS NULL) OR (outcome = 'known' AND actual_usd_micro IS NULL AND actual_allowance_micro IS NULL AND evidence_id IS NOT NULL) OR (outcome = 'unknown' AND actual_usd_micro IS NULL AND actual_allowance_micro IS NULL AND evidence_id IS NULL)", name="ledger_receipt_branch"),
        CheckConstraint("(actual_usd_micro IS NULL OR actual_usd_micro >= 0) AND (actual_allowance_micro IS NULL OR actual_allowance_micro >= 0) AND currency = 'USD' AND observed_ms >= 0 AND custody_bytes > 0 AND custody_bytes <= 1048576", name="ledger_receipt_bounds"))

    link = Table("program_legacy_links", metadata,
        ident("scope_id", pk=True), ident("batch_id", pk=True), ident("link_id", pk=True),
        Column("job_id", String(64), nullable=False), ident("original_attempt_id"), ident("original_call_id"),
        ident("original_slot_id"), digest("original_digest"), ident("subject_id"), ident("call_id"), ident("slot_id"),
        Column("baseline_state", String(24), nullable=False), Column("baseline_slot_state", String(24), nullable=False),
        amount("baseline_reserved_usd_micro"), amount("baseline_reserved_allowance_micro"),
        amount("baseline_actual_usd_micro", nullable=True), amount("baseline_actual_allowance_micro", nullable=True),
        Column("audit_sequence", Integer, nullable=False),
        fk(["scope_id", "batch_id"], ["program_census_batches.scope_id", "program_census_batches.batch_id"], "ledger_link_batch_fk"),
        fk(["job_id"], ["jobs.id"], "ledger_link_job_fk"),
        fk(["scope_id", "call_id", "subject_id"], ["program_reservations.scope_id", "program_reservations.call_id", "program_reservations.subject_id"], "ledger_link_reservation_fk"),
        fk(["scope_id", "slot_id"], ["program_slots.scope_id", "program_slots.slot_id"], "ledger_link_slot_fk"),
        audit("ledger_link_audit_fk"),
        UniqueConstraint("scope_id", "batch_id", "job_id", "original_attempt_id", "original_call_id", name="ledger_link_source_call_key"),
        UniqueConstraint("scope_id", "batch_id", "job_id", "original_attempt_id", "original_slot_id", name="ledger_link_source_slot_key"),
        UniqueConstraint("scope_id", "batch_id", "call_id", name="ledger_link_call_key"),
        UniqueConstraint("scope_id", "batch_id", "slot_id", name="ledger_link_slot_key"),
        CheckConstraint("baseline_reserved_usd_micro >= 0 AND baseline_reserved_allowance_micro >= 0", name="ledger_link_reserve_bounds"),
        CheckConstraint("(baseline_state = 'reserved' AND baseline_slot_state = 'held' AND baseline_actual_usd_micro IS NULL AND baseline_actual_allowance_micro IS NULL) OR (baseline_state = 'settled' AND baseline_slot_state = 'released' AND baseline_actual_usd_micro IS NOT NULL AND baseline_actual_usd_micro >= 0 AND baseline_actual_allowance_micro IS NOT NULL AND baseline_actual_allowance_micro >= 0) OR (baseline_state = 'unknown' AND baseline_slot_state = 'unknown' AND ((baseline_actual_usd_micro IS NULL AND baseline_actual_allowance_micro IS NULL) OR (baseline_actual_usd_micro IS NOT NULL AND baseline_actual_usd_micro >= 0 AND baseline_actual_allowance_micro IS NOT NULL AND baseline_actual_allowance_micro >= 0)))", name="ledger_link_baseline_branch"))

    # Reverse lookups for owner locking and retention checks; not authority.
    Index("ledger_batch_scope_state_idx", batch.c.scope_id, batch.c.state)
    Index("ledger_registry_scope_purpose_idx", registry.c.scope_id, registry.c.purpose)
    Index("ledger_intent_subject_state_idx", intent.c.scope_id, intent.c.subject_id, intent.c.state)
    Index("ledger_intent_job_idx", intent.c.work_job_id)
    Index("ledger_intent_epoch_idx", intent.c.collection_session_id, intent.c.collection_epoch)
    Index("ledger_evidence_scope_kind_idx", evidence.c.scope_id, evidence.c.kind)
    Index("ledger_receipt_call_idx", receipt.c.scope_id, receipt.c.call_id)
    Index("ledger_link_job_idx", link.c.job_id)
    Index("ledger_link_reservation_idx", link.c.scope_id, link.c.call_id, link.c.subject_id)
    Index("ledger_link_slot_idx", link.c.scope_id, link.c.slot_id)

    return policy, batch, gate, registry, intent, evidence, receipt, link
