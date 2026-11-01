BEGIN TRANSACTION;
CREATE TABLE audit_events (
	sequence INTEGER NOT NULL,
	created_at FLOAT NOT NULL,
	actor VARCHAR(160) NOT NULL,
	action VARCHAR(100) NOT NULL,
	target VARCHAR(160) NOT NULL,
	detail JSON NOT NULL,
	previous_hash VARCHAR(64) NOT NULL,
	hash VARCHAR(64) NOT NULL,
	PRIMARY KEY (sequence)
);
INSERT INTO "audit_events" VALUES(1,1793556438.9733727,'fixture','legacy-preserve','historical','{"marker": "v8"}','0000000000000000000000000000000000000000000000000000000000000000','fd6cedbb4d53402fbdcc7b7912f445ad6356175304928cbd47c60095eeb89e87');
CREATE TABLE auth_sessions (
	digest VARCHAR(64) NOT NULL,
	identity VARCHAR(160) NOT NULL,
	role VARCHAR(32) NOT NULL,
	expires_at FLOAT NOT NULL,
	PRIMARY KEY (digest)
);
CREATE TABLE binding_runtime_state (
	id VARCHAR(160) NOT NULL,
	inhibited BOOLEAN NOT NULL,
	reason VARCHAR(500) NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE bundle_revisions (
	generation VARCHAR(80) NOT NULL,
	bundle JSON NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (generation)
);
CREATE TABLE collection_acknowledgements (
	enrollment_id VARCHAR(96) NOT NULL,
	sequence BIGINT NOT NULL,
	session_id VARCHAR(96) NOT NULL,
	operation_id VARCHAR(96) NOT NULL,
	ack_digest VARCHAR(64) NOT NULL,
	key_id VARCHAR(96) NOT NULL,
	canonical_payload TEXT NOT NULL,
	signature TEXT NOT NULL,
	custody_ref VARCHAR(512) NOT NULL,
	event VARCHAR(24) NOT NULL,
	outcome VARCHAR(24) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (enrollment_id, sequence),
	CONSTRAINT collection_ack_operation_fk FOREIGN KEY(session_id, operation_id, enrollment_id) REFERENCES collection_operations (session_id, operation_id, enrollment_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_ack_enrollment_fk FOREIGN KEY(enrollment_id, session_id) REFERENCES collection_enrollments (id, session_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_ack_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_ack_digest_unique UNIQUE (ack_digest),
	CONSTRAINT collection_ack_session_key UNIQUE (enrollment_id, sequence, session_id),
	CONSTRAINT collection_ack_sequence_positive CHECK (sequence > 0),
	CONSTRAINT collection_ack_event CHECK (event IN ('STARTED', 'COMPLETED', 'QUIESCED')),
	CONSTRAINT collection_ack_outcome CHECK (outcome IN ('known', 'unknown'))
);
CREATE TABLE collection_checkpoints (
	session_id VARCHAR(96) NOT NULL,
	sequence INTEGER NOT NULL,
	artifact_digest VARCHAR(64) NOT NULL,
	custody_ref VARCHAR(512) NOT NULL,
	retain_until_ms BIGINT NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (session_id, sequence),
	CONSTRAINT collection_checkpoint_session_fk FOREIGN KEY(session_id) REFERENCES collection_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_checkpoint_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_checkpoint_bounds CHECK (sequence > 0 AND retain_until_ms > 0)
);
CREATE TABLE collection_enrollments (
	id VARCHAR(96) NOT NULL,
	session_id VARCHAR(96) NOT NULL,
	original_epoch INTEGER NOT NULL,
	resource_digest VARCHAR(64) NOT NULL,
	boot_digest VARCHAR(64) NOT NULL,
	permit_digest VARCHAR(64) NOT NULL,
	ack_key_digest VARCHAR(64) NOT NULL,
	last_ack_sequence BIGINT NOT NULL,
	retain_until_ms BIGINT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT collection_enrollment_epoch_fk FOREIGN KEY(session_id, original_epoch) REFERENCES collection_epochs (session_id, epoch) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_enrollment_session_key UNIQUE (id, session_id),
	CONSTRAINT collection_enrollment_bounds CHECK (last_ack_sequence >= 0 AND retain_until_ms > 0)
);
CREATE TABLE collection_epoch_guards (
	session_id VARCHAR(96) NOT NULL,
	active_epoch INTEGER,
	revision INTEGER NOT NULL,
	PRIMARY KEY (session_id),
	CONSTRAINT collection_guard_session_fk FOREIGN KEY(session_id) REFERENCES collection_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_guard_epoch_fk FOREIGN KEY(session_id, active_epoch) REFERENCES collection_epochs (session_id, epoch) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_guard_revision CHECK (revision >= 0)
);
CREATE TABLE collection_epochs (
	session_id VARCHAR(96) NOT NULL,
	epoch INTEGER NOT NULL,
	epoch_purpose VARCHAR(24) NOT NULL,
	principal_id VARCHAR(96) NOT NULL,
	authorization_digest VARCHAR(64) NOT NULL,
	deadline_ms BIGINT NOT NULL,
	retain_until_ms BIGINT NOT NULL,
	state VARCHAR(24) NOT NULL,
	started_at_ms BIGINT,
	ended_at_ms BIGINT,
	PRIMARY KEY (session_id, epoch),
	CONSTRAINT collection_epoch_session_fk FOREIGN KEY(session_id) REFERENCES collection_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_epoch_purpose_key UNIQUE (session_id, epoch, epoch_purpose),
	CONSTRAINT collection_epoch_bounds CHECK (epoch > 0 AND deadline_ms > 0 AND retain_until_ms >= deadline_ms),
	CONSTRAINT collection_epoch_purpose CHECK (epoch_purpose IN ('execution', 'reconciliation', 'recovery')),
	CONSTRAINT collection_epoch_state CHECK (state IN ('authorized', 'active', 'closed', 'unknown'))
);
CREATE TABLE collection_observations (
	session_id VARCHAR(96) NOT NULL,
	sequence INTEGER NOT NULL,
	recipe_id VARCHAR(96) NOT NULL,
	recipe_version VARCHAR(96) NOT NULL,
	status VARCHAR(24) NOT NULL,
	payload_digest VARCHAR(64) NOT NULL,
	custody_ref VARCHAR(512) NOT NULL,
	retain_until_ms BIGINT NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (session_id, sequence),
	CONSTRAINT collection_observation_session_fk FOREIGN KEY(session_id) REFERENCES collection_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_observation_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_observation_recipe_unique UNIQUE (session_id, sequence, recipe_id, recipe_version),
	CONSTRAINT collection_observation_bounds CHECK (sequence > 0 AND retain_until_ms > 0),
	CONSTRAINT collection_observation_status CHECK (status IN ('passed', 'failed', 'not_run', 'unknown'))
);
CREATE TABLE collection_operations (
	session_id VARCHAR(96) NOT NULL,
	operation_id VARCHAR(96) NOT NULL,
	epoch INTEGER NOT NULL,
	epoch_purpose VARCHAR(24) NOT NULL,
	enrollment_id VARCHAR(96) NOT NULL,
	ordinal INTEGER NOT NULL,
	idempotency_key VARCHAR(96) NOT NULL,
	recipe_id VARCHAR(96) NOT NULL,
	verb VARCHAR(24) NOT NULL,
	session_digest VARCHAR(64) NOT NULL,
	request_digest VARCHAR(64) NOT NULL,
	resource_digest VARCHAR(64) NOT NULL,
	boot_digest VARCHAR(64) NOT NULL,
	nonce VARCHAR(128) NOT NULL,
	issuer_key_id VARCHAR(96) NOT NULL,
	start_not_after_ms BIGINT NOT NULL,
	execution_not_after_ms BIGINT NOT NULL,
	authorization_claims TEXT NOT NULL,
	authorization_claims_digest VARCHAR(64) NOT NULL,
	authorization_envelope TEXT,
	authorization_envelope_digest VARCHAR(64),
	state VARCHAR(24) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (session_id, operation_id),
	CONSTRAINT collection_operation_epoch_fk FOREIGN KEY(session_id, epoch, epoch_purpose) REFERENCES collection_epochs (session_id, epoch, epoch_purpose) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_operation_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_operation_enrollment_fk FOREIGN KEY(enrollment_id, session_id) REFERENCES collection_enrollments (id, session_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_operation_enrollment_key UNIQUE (session_id, operation_id, enrollment_id),
	CONSTRAINT collection_operation_idempotency UNIQUE (session_id, idempotency_key),
	CONSTRAINT collection_operation_nonce UNIQUE (session_id, nonce),
	CONSTRAINT collection_operation_ordinal UNIQUE (session_id, epoch, ordinal),
	CONSTRAINT collection_operation_purpose CHECK (epoch_purpose IN ('execution', 'reconciliation', 'recovery')),
	CONSTRAINT collection_operation_verb CHECK (verb IN ('seed-v1', 'exec-v1', 'quiesce-v1', 'export-v1')),
	CONSTRAINT collection_operation_state CHECK (state IN ('intent', 'acknowledged', 'unknown')),
	CONSTRAINT collection_operation_recovery_verb CHECK (epoch_purpose = 'execution' OR (epoch_purpose = 'recovery' AND verb IN ('quiesce-v1', 'export-v1'))),
	CONSTRAINT collection_operation_bounds CHECK (ordinal >= 0 AND start_not_after_ms > 0 AND execution_not_after_ms >= start_not_after_ms),
	CONSTRAINT collection_operation_signed_pair CHECK ((authorization_envelope IS NULL AND authorization_envelope_digest IS NULL) OR (authorization_envelope IS NOT NULL AND authorization_envelope_digest IS NOT NULL))
);
CREATE TABLE collection_retention_refs (
	session_id VARCHAR(96) NOT NULL,
	reference_id VARCHAR(96) NOT NULL,
	kind VARCHAR(24) NOT NULL,
	object_digest VARCHAR(64) NOT NULL,
	custody_ref VARCHAR(512) NOT NULL,
	retain_until_ms BIGINT NOT NULL,
	hold VARCHAR(24) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	checkpoint_sequence INTEGER,
	observation_sequence INTEGER,
	ack_enrollment_id VARCHAR(96),
	ack_sequence BIGINT,
	qualification_id VARCHAR(64),
	PRIMARY KEY (session_id, reference_id),
	CONSTRAINT collection_retention_checkpoint_fk FOREIGN KEY(session_id, checkpoint_sequence) REFERENCES collection_checkpoints (session_id, sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_retention_observation_fk FOREIGN KEY(session_id, observation_sequence) REFERENCES collection_observations (session_id, sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_retention_ack_fk FOREIGN KEY(ack_enrollment_id, ack_sequence, session_id) REFERENCES collection_acknowledgements (enrollment_id, sequence, session_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_retention_qualification_fk FOREIGN KEY(qualification_id) REFERENCES qualifications (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_retention_session_fk FOREIGN KEY(session_id) REFERENCES collection_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_retention_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_retention_kind CHECK (kind IN ('receipt', 'checkpoint', 'observation', 'qualification', 'resource')),
	CONSTRAINT collection_retention_hold CHECK (hold IN ('active', 'released')),
	CONSTRAINT collection_retention_bounds CHECK (retain_until_ms > 0),
	CONSTRAINT collection_retention_ack_pair CHECK ((ack_enrollment_id IS NULL AND ack_sequence IS NULL) OR (ack_enrollment_id IS NOT NULL AND ack_sequence IS NOT NULL)),
	CONSTRAINT collection_retention_receipt_required CHECK (kind <> 'receipt' OR ack_sequence IS NOT NULL),
	CONSTRAINT collection_retention_checkpoint_required CHECK (kind <> 'checkpoint' OR checkpoint_sequence IS NOT NULL),
	CONSTRAINT collection_retention_observation_required CHECK (kind <> 'observation' OR observation_sequence IS NOT NULL),
	CONSTRAINT collection_retention_qualification_required CHECK (kind <> 'qualification' OR qualification_id IS NOT NULL)
);
CREATE TABLE collection_sessions (
	id VARCHAR(96) NOT NULL,
	principal_id VARCHAR(96) NOT NULL,
	idempotency_key VARCHAR(96) NOT NULL,
	scope_id VARCHAR(96) NOT NULL,
	workspace_id VARCHAR(96) NOT NULL,
	repository_id VARCHAR(96) NOT NULL,
	schema_version VARCHAR(96) NOT NULL,
	purpose VARCHAR(32) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	suite_version VARCHAR(96) NOT NULL,
	suite_digest VARCHAR(64) NOT NULL,
	seed_digest VARCHAR(64) NOT NULL,
	base_sha VARCHAR(64) NOT NULL,
	paths_digest VARCHAR(64) NOT NULL,
	deployment_digest VARCHAR(64) NOT NULL,
	permit_digest VARCHAR(64) NOT NULL,
	immutable_digest VARCHAR(64) NOT NULL,
	immutable_payload JSON NOT NULL,
	deadline_ms BIGINT NOT NULL,
	retain_until_ms BIGINT NOT NULL,
	state VARCHAR(32) NOT NULL,
	revision INTEGER NOT NULL,
	current_epoch INTEGER,
	cancel_revision INTEGER,
	cancel_at_ms BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT collection_session_scope_fk FOREIGN KEY(scope_id) REFERENCES program_budget_scopes (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT collection_session_idempotency UNIQUE (principal_id, idempotency_key),
	CONSTRAINT collection_session_purpose CHECK (purpose IN ('operator_c0_collection')),
	CONSTRAINT collection_session_state CHECK (state IN ('created', 'admitted', 'active', 'quiescing', 'captured', 'complete', 'cancel_pending', 'cancelled', 'failed', 'stopped_unknown')),
	CONSTRAINT collection_session_bounds CHECK (revision >= 0 AND deadline_ms > 0 AND retain_until_ms >= deadline_ms),
	CONSTRAINT collection_session_cancel_pair CHECK ((cancel_revision IS NULL AND cancel_at_ms IS NULL) OR (cancel_revision IS NOT NULL AND cancel_at_ms IS NOT NULL)),
	CONSTRAINT collection_session_cancel_pending CHECK (state <> 'cancel_pending' OR cancel_revision IS NOT NULL),
	CONSTRAINT collection_session_cancelled CHECK (state <> 'cancelled' OR cancel_revision IS NOT NULL)
);
CREATE TABLE execution_authority (
	job_id VARCHAR(64) NOT NULL,
	fence VARCHAR(64) NOT NULL,
	capability_digest VARCHAR(64) NOT NULL,
	context JSON NOT NULL,
	expires_at FLOAT NOT NULL,
	deadline FLOAT NOT NULL,
	PRIMARY KEY (job_id)
);
CREATE TABLE grant_runtime_state (
	id VARCHAR(160) NOT NULL,
	inhibited BOOLEAN NOT NULL,
	reason VARCHAR(500) NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE helper_enrollments (
	id VARCHAR(160) NOT NULL,
	binding_id VARCHAR(160) NOT NULL,
	installation_id VARCHAR(160) NOT NULL,
	role VARCHAR(160) NOT NULL,
	expires_at FLOAT NOT NULL,
	certificate_digest VARCHAR(64) NOT NULL,
	public_key TEXT NOT NULL,
	revoked BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (certificate_digest)
);
CREATE TABLE helper_sessions (
	id VARCHAR(64) NOT NULL,
	binding_id VARCHAR(160) NOT NULL,
	enrollment_id VARCHAR(160) NOT NULL,
	gateway_id VARCHAR(64) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	challenge VARCHAR(64) NOT NULL,
	active BOOLEAN NOT NULL,
	identity JSON,
	last_seen FLOAT NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE inference_requests (
	id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	fence VARCHAR(64) NOT NULL,
	binding_id VARCHAR(160) NOT NULL,
	session_id VARCHAR(64) NOT NULL,
	gateway_id VARCHAR(64) NOT NULL,
	workload VARCHAR(160) NOT NULL,
	request JSON NOT NULL,
	permit_digest VARCHAR(64) NOT NULL,
	permit_until FLOAT NOT NULL,
	status VARCHAR(32) NOT NULL,
	outcome VARCHAR(80),
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	evidence JSON NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE jobs (
	id VARCHAR(64) NOT NULL,
	receipt VARCHAR(96) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	request_digest VARCHAR(80) NOT NULL,
	caller VARCHAR(160) NOT NULL,
	grant_id VARCHAR(160) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	resolved JSON NOT NULL,
	task TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	started_at FLOAT,
	finished_at FLOAT,
	attempts INTEGER NOT NULL,
	next_attempt_at FLOAT NOT NULL,
	lease_owner VARCHAR(96),
	lease_until FLOAT,
	cancel_requested BOOLEAN NOT NULL,
	artifact_key VARCHAR(160),
	artifact_sha256 VARCHAR(64),
	error_code VARCHAR(80),
	public_projection JSON,
	released_at FLOAT,
	PRIMARY KEY (id),
	UNIQUE (idempotency_key)
);
INSERT INTO "jobs" VALUES('historical','historical','historical','historical','historical','historical','historical','{"marker": ["a", 1]}','preserve','queued',1793556438.9748247,1793556438.9748249,NULL,NULL,0,1793556438.9748254,NULL,NULL,0,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE platform_metadata (
	"key" VARCHAR(80) NOT NULL,
	value JSON NOT NULL,
	PRIMARY KEY ("key")
);
INSERT INTO "platform_metadata" VALUES('schema_version','{"version": 8}');
INSERT INTO "platform_metadata" VALUES('audit_head','{"sequence": 1, "hash": "fd6cedbb4d53402fbdcc7b7912f445ad6356175304928cbd47c60095eeb89e87"}');
CREATE TABLE program_budget_scopes (
	id VARCHAR(96) NOT NULL,
	policy_revision INTEGER NOT NULL,
	window_start_ms BIGINT NOT NULL,
	window_end_ms BIGINT NOT NULL,
	usd_limit_micro BIGINT NOT NULL,
	allowance_unit VARCHAR(96) NOT NULL,
	allowance_limit_micro BIGINT NOT NULL,
	slot_limit INTEGER NOT NULL,
	policy_digest VARCHAR(64) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT program_scope_revision_positive CHECK (policy_revision > 0),
	CONSTRAINT program_scope_window_order CHECK (window_start_ms >= 0 AND window_end_ms > window_start_ms),
	CONSTRAINT program_scope_limits_nonnegative CHECK (usd_limit_micro >= 0 AND allowance_limit_micro >= 0 AND slot_limit >= 0)
);
INSERT INTO "program_budget_scopes" VALUES('scope',1,1,20,10,'opencode-go-allowance',20,1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
CREATE TABLE program_budget_subjects (
	scope_id VARCHAR(96) NOT NULL,
	subject_id VARCHAR(96) NOT NULL,
	purpose VARCHAR(24) NOT NULL,
	binding_digest VARCHAR(64) NOT NULL,
	PRIMARY KEY (scope_id, subject_id),
	CONSTRAINT program_subject_scope_fk FOREIGN KEY(scope_id) REFERENCES program_budget_scopes (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT program_subject_purpose_key UNIQUE (scope_id, subject_id, purpose),
	CONSTRAINT program_subject_purpose CHECK (purpose IN ('work', 'collection'))
);
INSERT INTO "program_budget_subjects" VALUES('scope','historical','work','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
CREATE TABLE program_reservations (
	scope_id VARCHAR(96) NOT NULL,
	call_id VARCHAR(96) NOT NULL,
	subject_id VARCHAR(96) NOT NULL,
	purpose VARCHAR(24) NOT NULL,
	effect_key VARCHAR(96) NOT NULL,
	reserved_usd_micro BIGINT NOT NULL,
	reserved_allowance_micro BIGINT NOT NULL,
	actual_usd_micro BIGINT,
	actual_allowance_micro BIGINT,
	permit_digest VARCHAR(64) NOT NULL,
	meter_digest VARCHAR(64) NOT NULL,
	price_digest VARCHAR(64) NOT NULL,
	permit_expires_ms BIGINT NOT NULL,
	state VARCHAR(24) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, call_id),
	CONSTRAINT program_reservation_subject_fk FOREIGN KEY(scope_id, subject_id, purpose) REFERENCES program_budget_subjects (scope_id, subject_id, purpose) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT program_reservation_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT program_reservation_subject_effect UNIQUE (scope_id, subject_id, effect_key),
	CONSTRAINT program_reservation_subject_call UNIQUE (scope_id, call_id, subject_id),
	CONSTRAINT program_reservation_purpose CHECK (purpose IN ('work', 'collection')),
	CONSTRAINT program_reservation_state CHECK (state IN ('reserved', 'settled', 'unknown')),
	CONSTRAINT program_reservation_bounds CHECK (reserved_usd_micro >= 0 AND reserved_allowance_micro >= 0 AND permit_expires_ms > 0),
	CONSTRAINT program_reservation_actual_usd CHECK (actual_usd_micro IS NULL OR actual_usd_micro >= 0),
	CONSTRAINT program_reservation_actual_allowance CHECK (actual_allowance_micro IS NULL OR actual_allowance_micro >= 0)
);
INSERT INTO "program_reservations" VALUES('scope','historical-call','historical','work','historical-effect',3,4,NULL,NULL,'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc','dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd','eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',10,'unknown',1);
CREATE TABLE program_slots (
	scope_id VARCHAR(96) NOT NULL,
	slot_id VARCHAR(96) NOT NULL,
	subject_id VARCHAR(96) NOT NULL,
	call_id VARCHAR(96) NOT NULL,
	state VARCHAR(24) NOT NULL,
	audit_sequence INTEGER NOT NULL,
	PRIMARY KEY (scope_id, slot_id),
	CONSTRAINT program_slot_subject_fk FOREIGN KEY(scope_id, subject_id) REFERENCES program_budget_subjects (scope_id, subject_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT program_slot_reservation_fk FOREIGN KEY(scope_id, call_id, subject_id) REFERENCES program_reservations (scope_id, call_id, subject_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT program_slot_audit_fk FOREIGN KEY(audit_sequence) REFERENCES audit_events (sequence) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT program_slot_call_unique UNIQUE (scope_id, call_id),
	CONSTRAINT program_slot_state CHECK (state IN ('held', 'released', 'unknown'))
);
INSERT INTO "program_slots" VALUES('scope','historical-slot','historical','historical-call','unknown',1);
CREATE TABLE qualifications (
	id VARCHAR(64) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	profile VARCHAR(160) NOT NULL,
	created_at FLOAT NOT NULL,
	expires_at FLOAT NOT NULL,
	passed BOOLEAN NOT NULL,
	issuer VARCHAR(160) NOT NULL,
	payload JSON NOT NULL,
	signature TEXT NOT NULL,
	public_key TEXT NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE resource_gateway_enrollments (
	id VARCHAR(64) NOT NULL,
	gateway_id VARCHAR(96) NOT NULL,
	audience VARCHAR(96) NOT NULL,
	tenant_id VARCHAR(128) NOT NULL,
	workspace_id VARCHAR(128) NOT NULL,
	domain_id VARCHAR(128) NOT NULL,
	token_digest VARCHAR(64) NOT NULL,
	expires_at FLOAT NOT NULL,
	revoked BOOLEAN NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (token_digest)
);
CREATE TABLE resource_output_grants (
	id VARCHAR(64) NOT NULL,
	workload_id VARCHAR(160) NOT NULL,
	gateway_id VARCHAR(96) NOT NULL,
	principal_id VARCHAR(160) NOT NULL,
	tenant_id VARCHAR(128) NOT NULL,
	workspace_id VARCHAR(128) NOT NULL,
	domain_id VARCHAR(128) NOT NULL,
	observation_id VARCHAR(128) NOT NULL,
	schema_version VARCHAR(96) NOT NULL,
	destination VARCHAR(96) NOT NULL,
	policy_revision VARCHAR(71) NOT NULL,
	active BOOLEAN NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE "resource_owner_requests" (
	id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	instance_id VARCHAR(64) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	workload_id VARCHAR(160) NOT NULL,
	gateway_id VARCHAR(96) NOT NULL,
	workload_token_digest VARCHAR(64) NOT NULL,
	principal_id VARCHAR(160) NOT NULL,
	fence VARCHAR(64) NOT NULL,
	tenant_id VARCHAR(128) NOT NULL,
	workspace_id VARCHAR(128) NOT NULL,
	domain_id VARCHAR(128) NOT NULL,
	observation_id VARCHAR(128) NOT NULL,
	namespace VARCHAR(63),
	pod_name VARCHAR(253),
	request JSON NOT NULL,
	state VARCHAR(24) NOT NULL,
	observation TEXT,
	observation_digest VARCHAR(64),
	release VARCHAR(24),
	enrollment_id VARCHAR(64),
	output_schema VARCHAR(96),
	output_destination VARCHAR(96),
	output_policy_revision VARCHAR(71),
	output_actor VARCHAR(160),
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT resource_request_target_shape CHECK ((namespace IS NULL AND pod_name IS NULL) OR (namespace IS NOT NULL AND pod_name IS NOT NULL))
);
CREATE TABLE "resource_workload_scopes" (
	workload_id VARCHAR(160) NOT NULL,
	gateway_id VARCHAR(96) NOT NULL,
	tenant_id VARCHAR(128) NOT NULL,
	workspace_id VARCHAR(128) NOT NULL,
	domain_id VARCHAR(128) NOT NULL,
	observation_id VARCHAR(128),
	namespace VARCHAR(63),
	pod_name VARCHAR(253),
	target_catalog JSON,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (workload_id),
	CONSTRAINT resource_scope_target_shape CHECK ((target_catalog IS NULL AND observation_id IS NOT NULL AND namespace IS NOT NULL AND pod_name IS NOT NULL) OR (target_catalog IS NOT NULL AND observation_id IS NULL AND namespace IS NULL AND pod_name IS NULL))
);
CREATE TABLE setup_drafts (
	id VARCHAR(64) NOT NULL,
	author VARCHAR(64) NOT NULL,
	installation_id VARCHAR(160) NOT NULL,
	idempotency_key VARCHAR(80) NOT NULL,
	request_digest VARCHAR(80) NOT NULL,
	base_generation VARCHAR(80) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	bundle JSON NOT NULL,
	state VARCHAR(32) NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (idempotency_key)
);
CREATE TABLE setup_principals (
	id VARCHAR(64) NOT NULL,
	name VARCHAR(160) NOT NULL,
	installation_id VARCHAR(160) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	envelope JSON NOT NULL,
	expires_at FLOAT NOT NULL,
	revoked BOOLEAN NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (token_hash)
);
CREATE TABLE worker_execution_leases (
	job_id VARCHAR(64) NOT NULL,
	workload_id VARCHAR(160) NOT NULL,
	instance_id VARCHAR(64) NOT NULL,
	fence VARCHAR(64) NOT NULL,
	generation VARCHAR(80) NOT NULL,
	phase VARCHAR(16) NOT NULL,
	lease_until FLOAT NOT NULL,
	execution_deadline FLOAT,
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (job_id)
);
CREATE TABLE worker_execution_operations (
	workload_id VARCHAR(160) NOT NULL,
	instance_id VARCHAR(64) NOT NULL,
	operation_id VARCHAR(64) NOT NULL,
	operation_kind VARCHAR(32) NOT NULL,
	request_digest VARCHAR(64) NOT NULL,
	job_id VARCHAR(64),
	fence VARCHAR(64),
	result JSON NOT NULL,
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (workload_id, instance_id, operation_id)
);
CREATE TABLE worker_heartbeats (
	id VARCHAR(96) NOT NULL,
	updated_at FLOAT NOT NULL,
	job_id VARCHAR(64),
	PRIMARY KEY (id)
);
CREATE INDEX ix_auth_sessions_expires_at ON auth_sessions (expires_at);
CREATE INDEX ix_jobs_grant_id ON jobs (grant_id);
CREATE UNIQUE INDEX ix_jobs_receipt ON jobs (receipt);
CREATE INDEX ix_jobs_caller ON jobs (caller);
CREATE INDEX ix_jobs_status ON jobs (status);
CREATE INDEX ix_qualifications_generation ON qualifications (generation);
CREATE INDEX ix_helper_sessions_binding_id ON helper_sessions (binding_id);
CREATE INDEX ix_helper_sessions_retention ON helper_sessions (active, last_seen, id);
CREATE INDEX ix_inference_requests_session_id ON inference_requests (session_id);
CREATE INDEX ix_inference_requests_binding_id ON inference_requests (binding_id);
CREATE INDEX ix_inference_requests_job_id ON inference_requests (job_id);
CREATE INDEX ix_inference_requests_retention ON inference_requests (status, updated_at, id);
CREATE INDEX ix_setup_drafts_author ON setup_drafts (author);
CREATE INDEX ix_resource_owner_requests_job_id ON resource_owner_requests (job_id);
CREATE INDEX ix_collection_sessions_owner ON collection_sessions (state, retain_until_ms);
CREATE INDEX ix_collection_epochs_owner ON collection_epochs (session_id, state);
CREATE INDEX ix_collection_enrollments_owner ON collection_enrollments (session_id, retain_until_ms);
CREATE INDEX ix_collection_operations_owner ON collection_operations (session_id, state);
CREATE INDEX ix_collection_acknowledgements_owner ON collection_acknowledgements (session_id, operation_id);
CREATE INDEX ix_collection_checkpoints_owner ON collection_checkpoints (retain_until_ms);
CREATE INDEX ix_collection_observations_owner ON collection_observations (retain_until_ms);
CREATE INDEX ix_program_reservations_owner ON program_reservations (scope_id, state);
CREATE INDEX ix_program_slots_owner ON program_slots (scope_id, state);
CREATE INDEX ix_collection_retention_refs_owner ON collection_retention_refs (hold, retain_until_ms);
COMMIT;
