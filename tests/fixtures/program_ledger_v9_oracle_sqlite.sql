-- Frozen Sovite v9 ledger oracle DDL (sqlite); see program_ledger_v9_oracle.json.


PRAGMA foreign_keys=ON;


CREATE TABLE program_policy_revisions (
	scope_id VARCHAR(96) NOT NULL,
	revision BIGINT NOT NULL,
	policy_digest VARCHAR(64) NOT NULL,
	previous_digest VARCHAR(64),
	policy_canonical TEXT NOT NULL,
	currency VARCHAR(3) NOT NULL,
	allowance_unit VARCHAR(96) NOT NULL,
	window_start_ms BIGINT NOT NULL,
	window_end_ms BIGINT NOT NULL,
	usd_limit_micro BIGINT NOT NULL,
	allowance_limit_micro BIGINT NOT NULL,
	slot_limit BIGINT NOT NULL,
	price_policy_digest VARCHAR(64) NOT NULL,
	meter_policy_digest VARCHAR(64) NOT NULL,
	permit_policy_digest VARCHAR(64) NOT NULL,
	manifest_digest VARCHAR(64) NOT NULL,
	source_identity_digest VARCHAR(64) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, revision),
	CONSTRAINT ledger_policy_scope_fk FOREIGN KEY(scope_id) REFERENCES program_budget_scopes (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_policy_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_policy_digest_key UNIQUE (scope_id, policy_digest),
	CONSTRAINT ledger_policy_binding_key UNIQUE (scope_id, revision, policy_digest),
	CONSTRAINT ledger_policy_window CHECK (revision > 0 AND window_start_ms >= 0 AND window_end_ms > window_start_ms),
	CONSTRAINT ledger_policy_limits CHECK (usd_limit_micro >= 0 AND allowance_limit_micro >= 0 AND slot_limit >= 0),
	CONSTRAINT ledger_policy_currency CHECK (currency = 'USD')
);


CREATE TABLE program_census_batches (
	scope_id VARCHAR(96) NOT NULL,
	batch_id VARCHAR(96) NOT NULL,
	policy_revision BIGINT NOT NULL,
	policy_digest VARCHAR(64) NOT NULL,
	census_generation VARCHAR(32) NOT NULL,
	snapshot_digest VARCHAR(64),
	fence_digest VARCHAR(64),
	owner_epoch BIGINT,
	state VARCHAR(24) NOT NULL,
	source_count BIGINT NOT NULL,
	mapped_count BIGINT NOT NULL,
	unmatched_count BIGINT NOT NULL,
	reserved_usd_micro BIGINT NOT NULL,
	settled_usd_micro BIGINT NOT NULL,
	unknown_usd_micro BIGINT NOT NULL,
	reserved_allowance_micro BIGINT NOT NULL,
	settled_allowance_micro BIGINT NOT NULL,
	unknown_allowance_micro BIGINT NOT NULL,
	held_slots BIGINT NOT NULL,
	unknown_slots BIGINT NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, batch_id),
	CONSTRAINT ledger_batch_policy_fk FOREIGN KEY(scope_id, policy_revision, policy_digest) REFERENCES program_policy_revisions (scope_id, revision, policy_digest) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_batch_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_batch_state CHECK (state IN ('draft', 'complete', 'aborted')),
	CONSTRAINT ledger_batch_generation_key UNIQUE (scope_id, census_generation),
	CONSTRAINT ledger_batch_cutover_binding_key UNIQUE (scope_id, batch_id, census_generation, policy_revision, policy_digest, snapshot_digest, fence_digest, owner_epoch),
	CONSTRAINT ledger_batch_counts CHECK (source_count >= 0 AND mapped_count >= 0 AND unmatched_count >= 0),
	CONSTRAINT ledger_batch_totals CHECK (reserved_usd_micro >= 0 AND settled_usd_micro >= 0 AND unknown_usd_micro >= 0 AND reserved_allowance_micro >= 0 AND settled_allowance_micro >= 0 AND unknown_allowance_micro >= 0 AND held_slots >= 0 AND unknown_slots >= 0),
	CONSTRAINT ledger_batch_complete_binding CHECK (state <> 'complete' OR (snapshot_digest IS NOT NULL AND fence_digest IS NOT NULL AND owner_epoch IS NOT NULL AND owner_epoch >= 0))
);


CREATE INDEX ledger_batch_scope_state_idx ON program_census_batches (scope_id, state);


CREATE TABLE program_gate_state (
	scope_id VARCHAR(96) NOT NULL,
	policy_revision BIGINT NOT NULL,
	policy_digest VARCHAR(64) NOT NULL,
	mode VARCHAR(24) NOT NULL,
	revision BIGINT NOT NULL,
	cutover_batch_id VARCHAR(96),
	cutover_digest VARCHAR(64),
	cutover_generation VARCHAR(32),
	cutover_fence_digest VARCHAR(64),
	cutover_owner_epoch BIGINT,
	reason_code VARCHAR(96),
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id),
	CONSTRAINT ledger_gate_policy_fk FOREIGN KEY(scope_id, policy_revision, policy_digest) REFERENCES program_policy_revisions (scope_id, revision, policy_digest) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_gate_batch_fk FOREIGN KEY(scope_id, cutover_batch_id, cutover_generation, policy_revision, policy_digest, cutover_digest, cutover_fence_digest, cutover_owner_epoch) REFERENCES program_census_batches (scope_id, batch_id, census_generation, policy_revision, policy_digest, snapshot_digest, fence_digest, owner_epoch) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_gate_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_gate_mode CHECK (mode IN ('disabled', 'census', 'active', 'inhibited') AND revision >= 0),
	CONSTRAINT ledger_gate_cutover_pair CHECK ((cutover_batch_id IS NULL AND cutover_digest IS NULL AND cutover_generation IS NULL AND cutover_fence_digest IS NULL AND cutover_owner_epoch IS NULL) OR (cutover_batch_id IS NOT NULL AND cutover_digest IS NOT NULL AND cutover_generation IS NOT NULL AND cutover_fence_digest IS NOT NULL AND cutover_owner_epoch IS NOT NULL AND cutover_owner_epoch >= 0)),
	CONSTRAINT ledger_gate_active_batch CHECK (mode <> 'active' OR cutover_batch_id IS NOT NULL)
);


CREATE TABLE program_subject_registry (
	purpose VARCHAR(24) NOT NULL,
	subject_id VARCHAR(96) NOT NULL,
	scope_id VARCHAR(96) NOT NULL,
	binding_digest VARCHAR(64) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (purpose, subject_id),
	CONSTRAINT ledger_registry_subject_fk FOREIGN KEY(scope_id, subject_id, purpose) REFERENCES program_budget_subjects (scope_id, subject_id, purpose) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_registry_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_registry_global_subject_key UNIQUE (subject_id),
	CONSTRAINT ledger_registry_binding_key UNIQUE (scope_id, subject_id, purpose),
	CONSTRAINT ledger_registry_purpose CHECK (purpose IN ('work', 'collection'))
);


CREATE INDEX ledger_registry_scope_purpose_idx ON program_subject_registry (scope_id, purpose);


CREATE TABLE program_call_intents (
	scope_id VARCHAR(96) NOT NULL,
	call_id VARCHAR(96) NOT NULL,
	subject_id VARCHAR(96) NOT NULL,
	purpose VARCHAR(24) NOT NULL,
	effect_key VARCHAR(96) NOT NULL,
	idempotency_key VARCHAR(96) NOT NULL,
	request_digest VARCHAR(64) NOT NULL,
	policy_revision BIGINT NOT NULL,
	policy_digest VARCHAR(64) NOT NULL,
	price_digest VARCHAR(64) NOT NULL,
	meter_digest VARCHAR(64) NOT NULL,
	permit_digest VARCHAR(64) NOT NULL,
	source_code VARCHAR(96) NOT NULL,
	issuer_code VARCHAR(96) NOT NULL,
	source_identity_digest VARCHAR(64) NOT NULL,
	observed_ms BIGINT NOT NULL,
	valid_until_ms BIGINT NOT NULL,
	state VARCHAR(24) NOT NULL,
	work_job_id VARCHAR(64),
	work_attempt_id VARCHAR(96),
	work_lease_digest VARCHAR(64),
	collection_session_id VARCHAR(96),
	collection_epoch INTEGER,
	collection_context_digest VARCHAR(64),
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, call_id),
	CONSTRAINT ledger_intent_reservation_fk FOREIGN KEY(scope_id, call_id, subject_id) REFERENCES program_reservations (scope_id, call_id, subject_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_intent_registry_fk FOREIGN KEY(scope_id, subject_id, purpose) REFERENCES program_subject_registry (scope_id, subject_id, purpose) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_intent_policy_fk FOREIGN KEY(scope_id, policy_revision, policy_digest) REFERENCES program_policy_revisions (scope_id, revision, policy_digest) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_intent_job_fk FOREIGN KEY(work_job_id) REFERENCES jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_intent_epoch_fk FOREIGN KEY(collection_session_id, collection_epoch) REFERENCES collection_epochs (session_id, epoch) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_intent_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_intent_idempotency_key UNIQUE (scope_id, purpose, subject_id, idempotency_key),
	CONSTRAINT ledger_intent_effect_key UNIQUE (scope_id, subject_id, effect_key),
	CONSTRAINT ledger_intent_state CHECK (state IN ('committed', 'known', 'unknown')),
	CONSTRAINT ledger_intent_time CHECK (observed_ms >= 0 AND valid_until_ms > observed_ms),
	CONSTRAINT ledger_intent_context CHECK ((purpose = 'work' AND work_job_id IS NOT NULL AND work_attempt_id IS NOT NULL AND work_lease_digest IS NOT NULL AND subject_id = work_job_id AND collection_session_id IS NULL AND collection_epoch IS NULL AND collection_context_digest IS NULL) OR (purpose = 'collection' AND work_job_id IS NULL AND work_attempt_id IS NULL AND work_lease_digest IS NULL AND collection_session_id IS NOT NULL AND collection_epoch IS NOT NULL AND collection_epoch > 0 AND collection_context_digest IS NOT NULL AND subject_id = collection_session_id))
);


CREATE INDEX ledger_intent_epoch_idx ON program_call_intents (collection_session_id, collection_epoch);


CREATE INDEX ledger_intent_job_idx ON program_call_intents (work_job_id);


CREATE INDEX ledger_intent_subject_state_idx ON program_call_intents (scope_id, subject_id, state);


CREATE TABLE program_overflow_evidence (
	scope_id VARCHAR(96) NOT NULL,
	evidence_id VARCHAR(96) NOT NULL,
	kind VARCHAR(24) NOT NULL,
	call_id VARCHAR(96),
	receipt_digest VARCHAR(64),
	policy_revision BIGINT,
	policy_digest VARCHAR(64),
	snapshot_digest VARCHAR(64),
	usd_decimal VARCHAR(128) NOT NULL,
	allowance_decimal VARCHAR(128) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	allowance_unit VARCHAR(96) NOT NULL,
	source_digest VARCHAR(64) NOT NULL,
	custody_ref VARCHAR(512) NOT NULL,
	custody_bytes BIGINT NOT NULL,
	observed_ms BIGINT NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, evidence_id),
	CONSTRAINT ledger_evidence_scope_fk FOREIGN KEY(scope_id) REFERENCES program_budget_scopes (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_evidence_intent_fk FOREIGN KEY(scope_id, call_id) REFERENCES program_call_intents (scope_id, call_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_evidence_policy_fk FOREIGN KEY(scope_id, policy_revision, policy_digest) REFERENCES program_policy_revisions (scope_id, revision, policy_digest) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_evidence_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_evidence_receipt_binding UNIQUE (scope_id, call_id, receipt_digest, evidence_id),
	CONSTRAINT ledger_evidence_receipt_unique UNIQUE (scope_id, call_id, receipt_digest),
	CONSTRAINT ledger_evidence_snapshot_unique UNIQUE (scope_id, policy_revision, policy_digest, snapshot_digest),
	CONSTRAINT ledger_evidence_branch CHECK ((kind = 'call' AND call_id IS NOT NULL AND receipt_digest IS NOT NULL AND policy_revision IS NULL AND policy_digest IS NULL AND snapshot_digest IS NULL) OR (kind = 'aggregate' AND call_id IS NULL AND receipt_digest IS NULL AND policy_revision IS NOT NULL AND policy_digest IS NOT NULL AND snapshot_digest IS NOT NULL)),
	CONSTRAINT ledger_evidence_bounds CHECK (currency = 'USD' AND custody_bytes > 0 AND custody_bytes <= 1048576 AND observed_ms >= 0)
);


CREATE INDEX ledger_evidence_scope_kind_idx ON program_overflow_evidence (scope_id, kind);


CREATE TABLE program_call_receipts (
	scope_id VARCHAR(96) NOT NULL,
	call_id VARCHAR(96) NOT NULL,
	receipt_id VARCHAR(96) NOT NULL,
	receipt_digest VARCHAR(64) NOT NULL,
	outcome VARCHAR(24) NOT NULL,
	actual_usd_micro BIGINT,
	actual_allowance_micro BIGINT,
	evidence_id VARCHAR(96),
	currency VARCHAR(3) NOT NULL,
	allowance_unit VARCHAR(96) NOT NULL,
	source_code VARCHAR(96) NOT NULL,
	issuer_code VARCHAR(96) NOT NULL,
	observed_ms BIGINT NOT NULL,
	custody_ref VARCHAR(512) NOT NULL,
	source_digest VARCHAR(64) NOT NULL,
	source_identity_digest VARCHAR(64) NOT NULL,
	custody_bytes BIGINT NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, call_id, receipt_id),
	CONSTRAINT ledger_receipt_intent_fk FOREIGN KEY(scope_id, call_id) REFERENCES program_call_intents (scope_id, call_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_receipt_evidence_fk FOREIGN KEY(scope_id, call_id, receipt_digest, evidence_id) REFERENCES program_overflow_evidence (scope_id, call_id, receipt_digest, evidence_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_receipt_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_receipt_digest_key UNIQUE (receipt_digest),
	CONSTRAINT ledger_receipt_branch CHECK ((outcome = 'known' AND actual_usd_micro IS NOT NULL AND actual_allowance_micro IS NOT NULL AND evidence_id IS NULL) OR (outcome = 'known' AND actual_usd_micro IS NULL AND actual_allowance_micro IS NULL AND evidence_id IS NOT NULL) OR (outcome = 'unknown' AND actual_usd_micro IS NULL AND actual_allowance_micro IS NULL AND evidence_id IS NULL)),
	CONSTRAINT ledger_receipt_bounds CHECK ((actual_usd_micro IS NULL OR actual_usd_micro >= 0) AND (actual_allowance_micro IS NULL OR actual_allowance_micro >= 0) AND currency = 'USD' AND observed_ms >= 0 AND custody_bytes > 0 AND custody_bytes <= 1048576)
);


CREATE INDEX ledger_receipt_call_idx ON program_call_receipts (scope_id, call_id);


CREATE TABLE program_legacy_links (
	scope_id VARCHAR(96) NOT NULL,
	batch_id VARCHAR(96) NOT NULL,
	link_id VARCHAR(96) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	original_attempt_id VARCHAR(96) NOT NULL,
	original_call_id VARCHAR(96) NOT NULL,
	original_slot_id VARCHAR(96) NOT NULL,
	original_digest VARCHAR(64) NOT NULL,
	subject_id VARCHAR(96) NOT NULL,
	call_id VARCHAR(96) NOT NULL,
	slot_id VARCHAR(96) NOT NULL,
	baseline_state VARCHAR(24) NOT NULL,
	baseline_slot_state VARCHAR(24) NOT NULL,
	baseline_reserved_usd_micro BIGINT NOT NULL,
	baseline_reserved_allowance_micro BIGINT NOT NULL,
	baseline_actual_usd_micro BIGINT,
	baseline_actual_allowance_micro BIGINT,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, batch_id, link_id),
	CONSTRAINT ledger_link_batch_fk FOREIGN KEY(scope_id, batch_id) REFERENCES program_census_batches (scope_id, batch_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_link_job_fk FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_link_reservation_fk FOREIGN KEY(scope_id, call_id, subject_id) REFERENCES program_reservations (scope_id, call_id, subject_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_link_slot_fk FOREIGN KEY(scope_id, slot_id) REFERENCES program_slots (scope_id, slot_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_link_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ledger_link_source_call_key UNIQUE (scope_id, batch_id, job_id, original_attempt_id, original_call_id),
	CONSTRAINT ledger_link_source_slot_key UNIQUE (scope_id, batch_id, job_id, original_attempt_id, original_slot_id),
	CONSTRAINT ledger_link_call_key UNIQUE (scope_id, batch_id, call_id),
	CONSTRAINT ledger_link_slot_key UNIQUE (scope_id, batch_id, slot_id),
	CONSTRAINT ledger_link_reserve_bounds CHECK (baseline_reserved_usd_micro >= 0 AND baseline_reserved_allowance_micro >= 0),
	CONSTRAINT ledger_link_baseline_branch CHECK ((baseline_state = 'reserved' AND baseline_slot_state = 'held' AND baseline_actual_usd_micro IS NULL AND baseline_actual_allowance_micro IS NULL) OR (baseline_state = 'settled' AND baseline_slot_state = 'released' AND baseline_actual_usd_micro IS NOT NULL AND baseline_actual_usd_micro >= 0 AND baseline_actual_allowance_micro IS NOT NULL AND baseline_actual_allowance_micro >= 0) OR (baseline_state = 'unknown' AND baseline_slot_state = 'unknown' AND ((baseline_actual_usd_micro IS NULL AND baseline_actual_allowance_micro IS NULL) OR (baseline_actual_usd_micro IS NOT NULL AND baseline_actual_usd_micro >= 0 AND baseline_actual_allowance_micro IS NOT NULL AND baseline_actual_allowance_micro >= 0))))
);


CREATE INDEX ledger_link_job_idx ON program_legacy_links (job_id);


CREATE INDEX ledger_link_reservation_idx ON program_legacy_links (scope_id, call_id, subject_id);


CREATE INDEX ledger_link_slot_idx ON program_legacy_links (scope_id, slot_id);

-- Synthetic historical-only oracle rows. No provider, identity, or custody source exists.
INSERT INTO program_policy_revisions VALUES
('scope', 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', NULL, '{}', 'USD', 'opencode-go-allowance', 1, 20, 10, 20, 1, '1111111111111111111111111111111111111111111111111111111111111111', '2222222222222222222222222222222222222222222222222222222222222222', '3333333333333333333333333333333333333333333333333333333333333333', '4444444444444444444444444444444444444444444444444444444444444444', '5555555555555555555555555555555555555555555555555555555555555555', 1);
INSERT INTO program_census_batches VALUES
('scope', 'oracle-batch', 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '11111111111111111111111111111111', NULL, NULL, NULL, 'draft', 1, 1, 0, 3, 0, 0, 4, 0, 0, 1, 0, 1);
INSERT INTO program_gate_state VALUES
('scope', 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'disabled', 0, NULL, NULL, NULL, NULL, NULL, 'oracle-disabled', 1);
INSERT INTO program_subject_registry VALUES
('work', 'historical', 'scope', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 1);
INSERT INTO program_call_intents VALUES
('scope', 'historical-call', 'historical', 'work', 'historical-effect', 'oracle-idempotency', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '1111111111111111111111111111111111111111111111111111111111111111', '2222222222222222222222222222222222222222222222222222222222222222', '3333333333333333333333333333333333333333333333333333333333333333', 'fixture-source', 'fixture-issuer', '5555555555555555555555555555555555555555555555555555555555555555', 2, 10, 'committed', 'historical', 'oracle-attempt', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', NULL, NULL, NULL, 1);
INSERT INTO program_overflow_evidence VALUES
('scope', 'oracle-aggregate-evidence', 'aggregate', NULL, NULL, 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', '9223372036854775808', '4', 'USD', 'opencode-go-allowance', '6666666666666666666666666666666666666666666666666666666666666666', 'synthetic://oracle/aggregate', 17, 9, 1);
INSERT INTO program_call_receipts VALUES
('scope', 'historical-call', 'oracle-receipt', '7777777777777777777777777777777777777777777777777777777777777777', 'unknown', NULL, NULL, NULL, 'USD', 'opencode-go-allowance', 'fixture-source', 'fixture-issuer', 8, 'synthetic://oracle/receipt', '8888888888888888888888888888888888888888888888888888888888888888', '5555555555555555555555555555555555555555555555555555555555555555', 17, 1);
INSERT INTO program_legacy_links VALUES
('scope', 'oracle-batch', 'oracle-link', 'historical', 'oracle-attempt', 'legacy-call', 'legacy-slot', '9999999999999999999999999999999999999999999999999999999999999999', 'historical', 'historical-call', 'historical-slot', 'reserved', 'held', 3, 4, NULL, NULL, 1);
-- A settled known Work obligation: the historical reservation and released
-- slot are bound to a known intent and receipt for the existing synthetic job.
INSERT INTO program_reservations VALUES
('scope', 'oracle-settled-work-call', 'historical', 'work', 'oracle-settled-work-effect', 3, 4, 2, 3, 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 20, 'settled', 1);
INSERT INTO program_slots VALUES
('scope', 'oracle-settled-work-slot', 'historical', 'oracle-settled-work-call', 'released', 1);
INSERT INTO program_budget_subjects VALUES
('scope', 'oracle-collection', 'collection', 'abababababababababababababababababababababababababababababababab');
INSERT INTO program_subject_registry VALUES
('collection', 'oracle-collection', 'scope', 'abababababababababababababababababababababababababababababababab', 1);
INSERT INTO collection_sessions (id, principal_id, idempotency_key, scope_id, workspace_id, repository_id, schema_version, purpose, generation, suite_version, suite_digest, seed_digest, base_sha, paths_digest, deployment_digest, permit_digest, immutable_digest, immutable_payload, deadline_ms, retain_until_ms, state, revision, current_epoch, cancel_revision, cancel_at_ms) VALUES
('oracle-collection', 'oracle-principal', 'oracle-collection-idempotency', 'scope', 'synthetic-workspace', 'synthetic-repository', 'oracle-schema-v1', 'operator_c0_collection', 'oracle-generation', 'oracle-suite-v1', '1212121212121212121212121212121212121212121212121212121212121212', '2323232323232323232323232323232323232323232323232323232323232323', '3434343434343434343434343434343434343434343434343434343434343434', '4545454545454545454545454545454545454545454545454545454545454545', '5656565656565656565656565656565656565656565656565656565656565656', '6767676767676767676767676767676767676767676767676767676767676767', '7878787878787878787878787878787878787878787878787878787878787878', '{}', 100, 200, 'complete', 1, 1, NULL, NULL);
INSERT INTO collection_epochs (session_id, epoch, epoch_purpose, principal_id, authorization_digest, deadline_ms, retain_until_ms, state, started_at_ms, ended_at_ms) VALUES
('oracle-collection', 1, 'execution', 'oracle-principal', '8989898989898989898989898989898989898989898989898989898989898989', 100, 200, 'closed', 2, 3);
INSERT INTO program_reservations VALUES
('scope', 'oracle-collection-call', 'oracle-collection', 'collection', 'oracle-collection-effect', 2, 3, 1, 2, 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 20, 'settled', 1);
INSERT INTO program_slots VALUES
('scope', 'oracle-collection-slot', 'oracle-collection', 'oracle-collection-call', 'released', 1);
INSERT INTO program_call_intents VALUES
('scope', 'oracle-settled-work-call', 'historical', 'work', 'oracle-settled-work-effect', 'oracle-settled-work-idempotency', '9090909090909090909090909090909090909090909090909090909090909090', 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '1111111111111111111111111111111111111111111111111111111111111111', '2222222222222222222222222222222222222222222222222222222222222222', '3333333333333333333333333333333333333333333333333333333333333333', 'fixture-source', 'fixture-issuer', '5555555555555555555555555555555555555555555555555555555555555555', 4, 15, 'known', 'historical', 'oracle-settled-attempt', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', NULL, NULL, NULL, 1);
INSERT INTO program_call_intents VALUES
('scope', 'oracle-collection-call', 'oracle-collection', 'collection', 'oracle-collection-effect', 'oracle-collection-idempotency-call', 'a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0', 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '1111111111111111111111111111111111111111111111111111111111111111', '2222222222222222222222222222222222222222222222222222222222222222', '3333333333333333333333333333333333333333333333333333333333333333', 'fixture-source', 'fixture-issuer', '5656565656565656565656565656565656565656565656565656565656565656', 5, 16, 'known', NULL, NULL, NULL, 'oracle-collection', 1, 'b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0', 1);
INSERT INTO program_call_receipts VALUES
('scope', 'oracle-settled-work-call', 'oracle-settled-work-receipt', 'c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0', 'known', 2, 3, NULL, 'USD', 'opencode-go-allowance', 'fixture-source', 'fixture-issuer', 6, 'synthetic://oracle/settled-work-receipt', 'd0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0', '5555555555555555555555555555555555555555555555555555555555555555', 17, 1);
INSERT INTO program_call_receipts VALUES
('scope', 'oracle-collection-call', 'oracle-collection-receipt', 'e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0', 'known', 1, 2, NULL, 'USD', 'opencode-go-allowance', 'fixture-source', 'fixture-issuer', 7, 'synthetic://oracle/collection-receipt', 'f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0', '5656565656565656565656565656565656565656565656565656565656565656', 17, 1);
INSERT INTO collection_epoch_guards VALUES ('oracle-collection', NULL, 2);
UPDATE platform_metadata SET value = '{"version": 9}' WHERE key = 'schema_version';
