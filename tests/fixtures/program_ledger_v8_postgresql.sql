--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: __LEDGER_V8_SCHEMA__; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA __LEDGER_V8_SCHEMA__;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: audit_events; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.audit_events (
    sequence integer NOT NULL,
    created_at double precision NOT NULL,
    actor character varying(160) NOT NULL,
    action character varying(100) NOT NULL,
    target character varying(160) NOT NULL,
    detail json NOT NULL,
    previous_hash character varying(64) NOT NULL,
    hash character varying(64) NOT NULL
);


--
-- Name: auth_sessions; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.auth_sessions (
    digest character varying(64) NOT NULL,
    identity character varying(160) NOT NULL,
    role character varying(32) NOT NULL,
    expires_at double precision NOT NULL
);


--
-- Name: binding_runtime_state; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.binding_runtime_state (
    id character varying(160) NOT NULL,
    inhibited boolean NOT NULL,
    reason character varying(500) NOT NULL,
    updated_at double precision NOT NULL
);


--
-- Name: bundle_revisions; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.bundle_revisions (
    generation character varying(80) NOT NULL,
    bundle json NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: collection_acknowledgements; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_acknowledgements (
    enrollment_id character varying(96) NOT NULL,
    sequence bigint NOT NULL,
    session_id character varying(96) NOT NULL,
    operation_id character varying(96) NOT NULL,
    ack_digest character varying(64) NOT NULL,
    key_id character varying(96) NOT NULL,
    canonical_payload text NOT NULL,
    signature text NOT NULL,
    custody_ref character varying(512) NOT NULL,
    event character varying(24) NOT NULL,
    outcome character varying(24) NOT NULL,
    audit_sequence integer NOT NULL,
    CONSTRAINT collection_ack_event CHECK (((event)::text = ANY ((ARRAY['STARTED'::character varying, 'COMPLETED'::character varying, 'QUIESCED'::character varying])::text[]))),
    CONSTRAINT collection_ack_outcome CHECK (((outcome)::text = ANY ((ARRAY['known'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT collection_ack_sequence_positive CHECK ((sequence > 0))
);


--
-- Name: collection_checkpoints; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_checkpoints (
    session_id character varying(96) NOT NULL,
    sequence integer NOT NULL,
    artifact_digest character varying(64) NOT NULL,
    custody_ref character varying(512) NOT NULL,
    retain_until_ms bigint NOT NULL,
    audit_sequence integer NOT NULL,
    CONSTRAINT collection_checkpoint_bounds CHECK (((sequence > 0) AND (retain_until_ms > 0)))
);


--
-- Name: collection_enrollments; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_enrollments (
    id character varying(96) NOT NULL,
    session_id character varying(96) NOT NULL,
    original_epoch integer NOT NULL,
    resource_digest character varying(64) NOT NULL,
    boot_digest character varying(64) NOT NULL,
    permit_digest character varying(64) NOT NULL,
    ack_key_digest character varying(64) NOT NULL,
    last_ack_sequence bigint NOT NULL,
    retain_until_ms bigint NOT NULL,
    CONSTRAINT collection_enrollment_bounds CHECK (((last_ack_sequence >= 0) AND (retain_until_ms > 0)))
);


--
-- Name: collection_epoch_guards; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_epoch_guards (
    session_id character varying(96) NOT NULL,
    active_epoch integer,
    revision integer NOT NULL,
    CONSTRAINT collection_guard_revision CHECK ((revision >= 0))
);


--
-- Name: collection_epochs; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_epochs (
    session_id character varying(96) NOT NULL,
    epoch integer NOT NULL,
    epoch_purpose character varying(24) NOT NULL,
    principal_id character varying(96) NOT NULL,
    authorization_digest character varying(64) NOT NULL,
    deadline_ms bigint NOT NULL,
    retain_until_ms bigint NOT NULL,
    state character varying(24) NOT NULL,
    started_at_ms bigint,
    ended_at_ms bigint,
    CONSTRAINT collection_epoch_bounds CHECK (((epoch > 0) AND (deadline_ms > 0) AND (retain_until_ms >= deadline_ms))),
    CONSTRAINT collection_epoch_purpose CHECK (((epoch_purpose)::text = ANY ((ARRAY['execution'::character varying, 'reconciliation'::character varying, 'recovery'::character varying])::text[]))),
    CONSTRAINT collection_epoch_state CHECK (((state)::text = ANY ((ARRAY['authorized'::character varying, 'active'::character varying, 'closed'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: collection_observations; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_observations (
    session_id character varying(96) NOT NULL,
    sequence integer NOT NULL,
    recipe_id character varying(96) NOT NULL,
    recipe_version character varying(96) NOT NULL,
    status character varying(24) NOT NULL,
    payload_digest character varying(64) NOT NULL,
    custody_ref character varying(512) NOT NULL,
    retain_until_ms bigint NOT NULL,
    audit_sequence integer NOT NULL,
    CONSTRAINT collection_observation_bounds CHECK (((sequence > 0) AND (retain_until_ms > 0))),
    CONSTRAINT collection_observation_status CHECK (((status)::text = ANY ((ARRAY['passed'::character varying, 'failed'::character varying, 'not_run'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: collection_operations; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_operations (
    session_id character varying(96) NOT NULL,
    operation_id character varying(96) NOT NULL,
    epoch integer NOT NULL,
    epoch_purpose character varying(24) NOT NULL,
    enrollment_id character varying(96) NOT NULL,
    ordinal integer NOT NULL,
    idempotency_key character varying(96) NOT NULL,
    recipe_id character varying(96) NOT NULL,
    verb character varying(24) NOT NULL,
    session_digest character varying(64) NOT NULL,
    request_digest character varying(64) NOT NULL,
    resource_digest character varying(64) NOT NULL,
    boot_digest character varying(64) NOT NULL,
    nonce character varying(128) NOT NULL,
    issuer_key_id character varying(96) NOT NULL,
    start_not_after_ms bigint NOT NULL,
    execution_not_after_ms bigint NOT NULL,
    authorization_claims text NOT NULL,
    authorization_claims_digest character varying(64) NOT NULL,
    authorization_envelope text,
    authorization_envelope_digest character varying(64),
    state character varying(24) NOT NULL,
    audit_sequence integer NOT NULL,
    CONSTRAINT collection_operation_bounds CHECK (((ordinal >= 0) AND (start_not_after_ms > 0) AND (execution_not_after_ms >= start_not_after_ms))),
    CONSTRAINT collection_operation_purpose CHECK (((epoch_purpose)::text = ANY ((ARRAY['execution'::character varying, 'reconciliation'::character varying, 'recovery'::character varying])::text[]))),
    CONSTRAINT collection_operation_recovery_verb CHECK ((((epoch_purpose)::text = 'execution'::text) OR (((epoch_purpose)::text = 'recovery'::text) AND ((verb)::text = ANY ((ARRAY['quiesce-v1'::character varying, 'export-v1'::character varying])::text[]))))),
    CONSTRAINT collection_operation_signed_pair CHECK ((((authorization_envelope IS NULL) AND (authorization_envelope_digest IS NULL)) OR ((authorization_envelope IS NOT NULL) AND (authorization_envelope_digest IS NOT NULL)))),
    CONSTRAINT collection_operation_state CHECK (((state)::text = ANY ((ARRAY['intent'::character varying, 'acknowledged'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT collection_operation_verb CHECK (((verb)::text = ANY ((ARRAY['seed-v1'::character varying, 'exec-v1'::character varying, 'quiesce-v1'::character varying, 'export-v1'::character varying])::text[])))
);


--
-- Name: collection_retention_refs; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_retention_refs (
    session_id character varying(96) NOT NULL,
    reference_id character varying(96) NOT NULL,
    kind character varying(24) NOT NULL,
    object_digest character varying(64) NOT NULL,
    custody_ref character varying(512) NOT NULL,
    retain_until_ms bigint NOT NULL,
    hold character varying(24) NOT NULL,
    audit_sequence integer NOT NULL,
    checkpoint_sequence integer,
    observation_sequence integer,
    ack_enrollment_id character varying(96),
    ack_sequence bigint,
    qualification_id character varying(64),
    CONSTRAINT collection_retention_ack_pair CHECK ((((ack_enrollment_id IS NULL) AND (ack_sequence IS NULL)) OR ((ack_enrollment_id IS NOT NULL) AND (ack_sequence IS NOT NULL)))),
    CONSTRAINT collection_retention_bounds CHECK ((retain_until_ms > 0)),
    CONSTRAINT collection_retention_checkpoint_required CHECK ((((kind)::text <> 'checkpoint'::text) OR (checkpoint_sequence IS NOT NULL))),
    CONSTRAINT collection_retention_hold CHECK (((hold)::text = ANY ((ARRAY['active'::character varying, 'released'::character varying])::text[]))),
    CONSTRAINT collection_retention_kind CHECK (((kind)::text = ANY ((ARRAY['receipt'::character varying, 'checkpoint'::character varying, 'observation'::character varying, 'qualification'::character varying, 'resource'::character varying])::text[]))),
    CONSTRAINT collection_retention_observation_required CHECK ((((kind)::text <> 'observation'::text) OR (observation_sequence IS NOT NULL))),
    CONSTRAINT collection_retention_qualification_required CHECK ((((kind)::text <> 'qualification'::text) OR (qualification_id IS NOT NULL))),
    CONSTRAINT collection_retention_receipt_required CHECK ((((kind)::text <> 'receipt'::text) OR (ack_sequence IS NOT NULL)))
);


--
-- Name: collection_sessions; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.collection_sessions (
    id character varying(96) NOT NULL,
    principal_id character varying(96) NOT NULL,
    idempotency_key character varying(96) NOT NULL,
    scope_id character varying(96) NOT NULL,
    workspace_id character varying(96) NOT NULL,
    repository_id character varying(96) NOT NULL,
    schema_version character varying(96) NOT NULL,
    purpose character varying(32) NOT NULL,
    generation character varying(80) NOT NULL,
    suite_version character varying(96) NOT NULL,
    suite_digest character varying(64) NOT NULL,
    seed_digest character varying(64) NOT NULL,
    base_sha character varying(64) NOT NULL,
    paths_digest character varying(64) NOT NULL,
    deployment_digest character varying(64) NOT NULL,
    permit_digest character varying(64) NOT NULL,
    immutable_digest character varying(64) NOT NULL,
    immutable_payload json NOT NULL,
    deadline_ms bigint NOT NULL,
    retain_until_ms bigint NOT NULL,
    state character varying(32) NOT NULL,
    revision integer NOT NULL,
    current_epoch integer,
    cancel_revision integer,
    cancel_at_ms bigint,
    CONSTRAINT collection_session_bounds CHECK (((revision >= 0) AND (deadline_ms > 0) AND (retain_until_ms >= deadline_ms))),
    CONSTRAINT collection_session_cancel_pair CHECK ((((cancel_revision IS NULL) AND (cancel_at_ms IS NULL)) OR ((cancel_revision IS NOT NULL) AND (cancel_at_ms IS NOT NULL)))),
    CONSTRAINT collection_session_cancel_pending CHECK ((((state)::text <> 'cancel_pending'::text) OR (cancel_revision IS NOT NULL))),
    CONSTRAINT collection_session_cancelled CHECK ((((state)::text <> 'cancelled'::text) OR (cancel_revision IS NOT NULL))),
    CONSTRAINT collection_session_purpose CHECK (((purpose)::text = 'operator_c0_collection'::text)),
    CONSTRAINT collection_session_state CHECK (((state)::text = ANY ((ARRAY['created'::character varying, 'admitted'::character varying, 'active'::character varying, 'quiescing'::character varying, 'captured'::character varying, 'complete'::character varying, 'cancel_pending'::character varying, 'cancelled'::character varying, 'failed'::character varying, 'stopped_unknown'::character varying])::text[])))
);


--
-- Name: execution_authority; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.execution_authority (
    job_id character varying(64) NOT NULL,
    fence character varying(64) NOT NULL,
    capability_digest character varying(64) NOT NULL,
    context json NOT NULL,
    expires_at double precision NOT NULL,
    deadline double precision NOT NULL
);


--
-- Name: grant_runtime_state; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.grant_runtime_state (
    id character varying(160) NOT NULL,
    inhibited boolean NOT NULL,
    reason character varying(500) NOT NULL,
    updated_at double precision NOT NULL
);


--
-- Name: helper_enrollments; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.helper_enrollments (
    id character varying(160) NOT NULL,
    binding_id character varying(160) NOT NULL,
    installation_id character varying(160) NOT NULL,
    role character varying(160) NOT NULL,
    expires_at double precision NOT NULL,
    certificate_digest character varying(64) NOT NULL,
    public_key text NOT NULL,
    revoked boolean NOT NULL
);


--
-- Name: helper_sessions; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.helper_sessions (
    id character varying(64) NOT NULL,
    binding_id character varying(160) NOT NULL,
    enrollment_id character varying(160) NOT NULL,
    gateway_id character varying(64) NOT NULL,
    generation character varying(80) NOT NULL,
    challenge character varying(64) NOT NULL,
    active boolean NOT NULL,
    identity json,
    last_seen double precision NOT NULL
);


--
-- Name: inference_requests; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.inference_requests (
    id character varying(64) NOT NULL,
    job_id character varying(64) NOT NULL,
    fence character varying(64) NOT NULL,
    binding_id character varying(160) NOT NULL,
    session_id character varying(64) NOT NULL,
    gateway_id character varying(64) NOT NULL,
    workload character varying(160) NOT NULL,
    request json NOT NULL,
    permit_digest character varying(64) NOT NULL,
    permit_until double precision NOT NULL,
    status character varying(32) NOT NULL,
    outcome character varying(80),
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    evidence json NOT NULL
);


--
-- Name: jobs; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.jobs (
    id character varying(64) NOT NULL,
    receipt character varying(96) NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    request_digest character varying(80) NOT NULL,
    caller character varying(160) NOT NULL,
    grant_id character varying(160) NOT NULL,
    generation character varying(80) NOT NULL,
    resolved json NOT NULL,
    task text NOT NULL,
    status character varying(32) NOT NULL,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    started_at double precision,
    finished_at double precision,
    attempts integer NOT NULL,
    next_attempt_at double precision NOT NULL,
    lease_owner character varying(96),
    lease_until double precision,
    cancel_requested boolean NOT NULL,
    artifact_key character varying(160),
    artifact_sha256 character varying(64),
    error_code character varying(80),
    public_projection json,
    released_at double precision
);


--
-- Name: platform_metadata; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.platform_metadata (
    key character varying(80) NOT NULL,
    value json NOT NULL
);


--
-- Name: program_budget_scopes; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.program_budget_scopes (
    id character varying(96) NOT NULL,
    policy_revision integer NOT NULL,
    window_start_ms bigint NOT NULL,
    window_end_ms bigint NOT NULL,
    usd_limit_micro bigint NOT NULL,
    allowance_unit character varying(96) NOT NULL,
    allowance_limit_micro bigint NOT NULL,
    slot_limit integer NOT NULL,
    policy_digest character varying(64) NOT NULL,
    CONSTRAINT program_scope_limits_nonnegative CHECK (((usd_limit_micro >= 0) AND (allowance_limit_micro >= 0) AND (slot_limit >= 0))),
    CONSTRAINT program_scope_revision_positive CHECK ((policy_revision > 0)),
    CONSTRAINT program_scope_window_order CHECK (((window_start_ms >= 0) AND (window_end_ms > window_start_ms)))
);


--
-- Name: program_budget_subjects; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.program_budget_subjects (
    scope_id character varying(96) NOT NULL,
    subject_id character varying(96) NOT NULL,
    purpose character varying(24) NOT NULL,
    binding_digest character varying(64) NOT NULL,
    CONSTRAINT program_subject_purpose CHECK (((purpose)::text = ANY ((ARRAY['work'::character varying, 'collection'::character varying])::text[])))
);


--
-- Name: program_reservations; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.program_reservations (
    scope_id character varying(96) NOT NULL,
    call_id character varying(96) NOT NULL,
    subject_id character varying(96) NOT NULL,
    purpose character varying(24) NOT NULL,
    effect_key character varying(96) NOT NULL,
    reserved_usd_micro bigint NOT NULL,
    reserved_allowance_micro bigint NOT NULL,
    actual_usd_micro bigint,
    actual_allowance_micro bigint,
    permit_digest character varying(64) NOT NULL,
    meter_digest character varying(64) NOT NULL,
    price_digest character varying(64) NOT NULL,
    permit_expires_ms bigint NOT NULL,
    state character varying(24) NOT NULL,
    audit_sequence integer NOT NULL,
    CONSTRAINT program_reservation_actual_allowance CHECK (((actual_allowance_micro IS NULL) OR (actual_allowance_micro >= 0))),
    CONSTRAINT program_reservation_actual_usd CHECK (((actual_usd_micro IS NULL) OR (actual_usd_micro >= 0))),
    CONSTRAINT program_reservation_bounds CHECK (((reserved_usd_micro >= 0) AND (reserved_allowance_micro >= 0) AND (permit_expires_ms > 0))),
    CONSTRAINT program_reservation_purpose CHECK (((purpose)::text = ANY ((ARRAY['work'::character varying, 'collection'::character varying])::text[]))),
    CONSTRAINT program_reservation_state CHECK (((state)::text = ANY ((ARRAY['reserved'::character varying, 'settled'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: program_slots; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.program_slots (
    scope_id character varying(96) NOT NULL,
    slot_id character varying(96) NOT NULL,
    subject_id character varying(96) NOT NULL,
    call_id character varying(96) NOT NULL,
    state character varying(24) NOT NULL,
    audit_sequence integer NOT NULL,
    CONSTRAINT program_slot_state CHECK (((state)::text = ANY ((ARRAY['held'::character varying, 'released'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: qualifications; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.qualifications (
    id character varying(64) NOT NULL,
    generation character varying(80) NOT NULL,
    profile character varying(160) NOT NULL,
    created_at double precision NOT NULL,
    expires_at double precision NOT NULL,
    passed boolean NOT NULL,
    issuer character varying(160) NOT NULL,
    payload json NOT NULL,
    signature text NOT NULL,
    public_key text NOT NULL
);


--
-- Name: resource_gateway_enrollments; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.resource_gateway_enrollments (
    id character varying(64) NOT NULL,
    gateway_id character varying(96) NOT NULL,
    audience character varying(96) NOT NULL,
    tenant_id character varying(128) NOT NULL,
    workspace_id character varying(128) NOT NULL,
    domain_id character varying(128) NOT NULL,
    token_digest character varying(64) NOT NULL,
    expires_at double precision NOT NULL,
    revoked boolean NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: resource_output_grants; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.resource_output_grants (
    id character varying(64) NOT NULL,
    workload_id character varying(160) NOT NULL,
    gateway_id character varying(96) NOT NULL,
    principal_id character varying(160) NOT NULL,
    tenant_id character varying(128) NOT NULL,
    workspace_id character varying(128) NOT NULL,
    domain_id character varying(128) NOT NULL,
    observation_id character varying(128) NOT NULL,
    schema_version character varying(96) NOT NULL,
    destination character varying(96) NOT NULL,
    policy_revision character varying(71) NOT NULL,
    active boolean NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: resource_owner_requests; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.resource_owner_requests (
    id character varying(64) NOT NULL,
    job_id character varying(64) NOT NULL,
    instance_id character varying(64) NOT NULL,
    generation character varying(80) NOT NULL,
    workload_id character varying(160) NOT NULL,
    gateway_id character varying(96) NOT NULL,
    workload_token_digest character varying(64) NOT NULL,
    principal_id character varying(160) NOT NULL,
    fence character varying(64) NOT NULL,
    tenant_id character varying(128) NOT NULL,
    workspace_id character varying(128) NOT NULL,
    domain_id character varying(128) NOT NULL,
    observation_id character varying(128) NOT NULL,
    namespace character varying(63),
    pod_name character varying(253),
    request json NOT NULL,
    state character varying(24) NOT NULL,
    observation text,
    observation_digest character varying(64),
    release character varying(24),
    enrollment_id character varying(64),
    output_schema character varying(96),
    output_destination character varying(96),
    output_policy_revision character varying(71),
    output_actor character varying(160),
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    CONSTRAINT resource_request_target_shape CHECK ((((namespace IS NULL) AND (pod_name IS NULL)) OR ((namespace IS NOT NULL) AND (pod_name IS NOT NULL))))
);


--
-- Name: resource_workload_scopes; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.resource_workload_scopes (
    workload_id character varying(160) NOT NULL,
    gateway_id character varying(96) NOT NULL,
    tenant_id character varying(128) NOT NULL,
    workspace_id character varying(128) NOT NULL,
    domain_id character varying(128) NOT NULL,
    observation_id character varying(128),
    namespace character varying(63),
    pod_name character varying(253),
    updated_at double precision NOT NULL,
    target_catalog json,
    CONSTRAINT resource_scope_target_shape CHECK ((((target_catalog IS NULL) AND (observation_id IS NOT NULL) AND (namespace IS NOT NULL) AND (pod_name IS NOT NULL)) OR ((target_catalog IS NOT NULL) AND (observation_id IS NULL) AND (namespace IS NULL) AND (pod_name IS NULL))))
);


--
-- Name: setup_drafts; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.setup_drafts (
    id character varying(64) NOT NULL,
    author character varying(64) NOT NULL,
    installation_id character varying(160) NOT NULL,
    idempotency_key character varying(80) NOT NULL,
    request_digest character varying(80) NOT NULL,
    base_generation character varying(80) NOT NULL,
    generation character varying(80) NOT NULL,
    bundle json NOT NULL,
    state character varying(32) NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: setup_principals; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.setup_principals (
    id character varying(64) NOT NULL,
    name character varying(160) NOT NULL,
    installation_id character varying(160) NOT NULL,
    token_hash character varying(64) NOT NULL,
    envelope json NOT NULL,
    expires_at double precision NOT NULL,
    revoked boolean NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: worker_execution_leases; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.worker_execution_leases (
    job_id character varying(64) NOT NULL,
    workload_id character varying(160) NOT NULL,
    instance_id character varying(64) NOT NULL,
    fence character varying(64) NOT NULL,
    generation character varying(80) NOT NULL,
    phase character varying(16) NOT NULL,
    lease_until double precision NOT NULL,
    execution_deadline double precision,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL
);


--
-- Name: worker_execution_operations; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.worker_execution_operations (
    workload_id character varying(160) NOT NULL,
    instance_id character varying(64) NOT NULL,
    operation_id character varying(64) NOT NULL,
    operation_kind character varying(32) NOT NULL,
    request_digest character varying(64) NOT NULL,
    job_id character varying(64),
    fence character varying(64),
    result json NOT NULL,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL
);


--
-- Name: worker_heartbeats; Type: TABLE; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE TABLE __LEDGER_V8_SCHEMA__.worker_heartbeats (
    id character varying(96) NOT NULL,
    updated_at double precision NOT NULL,
    job_id character varying(64)
);


--
-- Data for Name: audit_events; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.audit_events (sequence, created_at, actor, action, target, detail, previous_hash, hash) VALUES (1, 1793556457.7620096, 'fixture', 'legacy-preserve', 'historical', '{"marker": "v8"}', '0000000000000000000000000000000000000000000000000000000000000000', 'adae9b40b8165b2babe894600a736874be281e74e689e7fcccdc2828f0479a80');


--
-- Data for Name: auth_sessions; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: binding_runtime_state; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: bundle_revisions; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_acknowledgements; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_checkpoints; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_enrollments; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_epoch_guards; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_epochs; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_observations; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_operations; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_retention_refs; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: collection_sessions; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: execution_authority; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: grant_runtime_state; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: helper_enrollments; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: helper_sessions; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: inference_requests; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: jobs; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.jobs (id, receipt, idempotency_key, request_digest, caller, grant_id, generation, resolved, task, status, created_at, updated_at, started_at, finished_at, attempts, next_attempt_at, lease_owner, lease_until, cancel_requested, artifact_key, artifact_sha256, error_code, public_projection, released_at) VALUES ('historical', 'historical', 'historical', 'historical', 'historical', 'historical', 'historical', '{"marker": ["a", 1]}', 'preserve', 'queued', 1793556457.7640011, 1793556457.7640016, NULL, NULL, 0, 1793556457.7640018, NULL, NULL, false, NULL, NULL, NULL, NULL, NULL);


--
-- Data for Name: platform_metadata; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.platform_metadata (key, value) VALUES ('schema_version', '{"version": 8}');
INSERT INTO __LEDGER_V8_SCHEMA__.platform_metadata (key, value) VALUES ('audit_head', '{"sequence": 1, "hash": "adae9b40b8165b2babe894600a736874be281e74e689e7fcccdc2828f0479a80"}');


--
-- Data for Name: program_budget_scopes; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.program_budget_scopes (id, policy_revision, window_start_ms, window_end_ms, usd_limit_micro, allowance_unit, allowance_limit_micro, slot_limit, policy_digest) VALUES ('scope', 1, 1, 20, 10, 'opencode-go-allowance', 20, 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');


--
-- Data for Name: program_budget_subjects; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.program_budget_subjects (scope_id, subject_id, purpose, binding_digest) VALUES ('scope', 'historical', 'work', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');


--
-- Data for Name: program_reservations; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.program_reservations (scope_id, call_id, subject_id, purpose, effect_key, reserved_usd_micro, reserved_allowance_micro, actual_usd_micro, actual_allowance_micro, permit_digest, meter_digest, price_digest, permit_expires_ms, state, audit_sequence) VALUES ('scope', 'historical-call', 'historical', 'work', 'historical-effect', 3, 4, NULL, NULL, 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 10, 'unknown', 1);


--
-- Data for Name: program_slots; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

INSERT INTO __LEDGER_V8_SCHEMA__.program_slots (scope_id, slot_id, subject_id, call_id, state, audit_sequence) VALUES ('scope', 'historical-slot', 'historical', 'historical-call', 'unknown', 1);


--
-- Data for Name: qualifications; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: resource_gateway_enrollments; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: resource_output_grants; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: resource_owner_requests; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: resource_workload_scopes; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: setup_drafts; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: setup_principals; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: worker_execution_leases; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: worker_execution_operations; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Data for Name: worker_heartbeats; Type: TABLE DATA; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--



--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (sequence);


--
-- Name: auth_sessions auth_sessions_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.auth_sessions
    ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (digest);


--
-- Name: binding_runtime_state binding_runtime_state_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.binding_runtime_state
    ADD CONSTRAINT binding_runtime_state_pkey PRIMARY KEY (id);


--
-- Name: bundle_revisions bundle_revisions_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.bundle_revisions
    ADD CONSTRAINT bundle_revisions_pkey PRIMARY KEY (generation);


--
-- Name: collection_acknowledgements collection_ack_digest_unique; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_acknowledgements
    ADD CONSTRAINT collection_ack_digest_unique UNIQUE (ack_digest);


--
-- Name: collection_acknowledgements collection_ack_session_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_acknowledgements
    ADD CONSTRAINT collection_ack_session_key UNIQUE (enrollment_id, sequence, session_id);


--
-- Name: collection_acknowledgements collection_acknowledgements_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_acknowledgements
    ADD CONSTRAINT collection_acknowledgements_pkey PRIMARY KEY (enrollment_id, sequence);


--
-- Name: collection_checkpoints collection_checkpoints_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_checkpoints
    ADD CONSTRAINT collection_checkpoints_pkey PRIMARY KEY (session_id, sequence);


--
-- Name: collection_enrollments collection_enrollment_session_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_enrollments
    ADD CONSTRAINT collection_enrollment_session_key UNIQUE (id, session_id);


--
-- Name: collection_enrollments collection_enrollments_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_enrollments
    ADD CONSTRAINT collection_enrollments_pkey PRIMARY KEY (id);


--
-- Name: collection_epoch_guards collection_epoch_guards_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_epoch_guards
    ADD CONSTRAINT collection_epoch_guards_pkey PRIMARY KEY (session_id);


--
-- Name: collection_epochs collection_epoch_purpose_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_epochs
    ADD CONSTRAINT collection_epoch_purpose_key UNIQUE (session_id, epoch, epoch_purpose);


--
-- Name: collection_epochs collection_epochs_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_epochs
    ADD CONSTRAINT collection_epochs_pkey PRIMARY KEY (session_id, epoch);


--
-- Name: collection_observations collection_observation_recipe_unique; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_observations
    ADD CONSTRAINT collection_observation_recipe_unique UNIQUE (session_id, sequence, recipe_id, recipe_version);


--
-- Name: collection_observations collection_observations_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_observations
    ADD CONSTRAINT collection_observations_pkey PRIMARY KEY (session_id, sequence);


--
-- Name: collection_operations collection_operation_enrollment_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_enrollment_key UNIQUE (session_id, operation_id, enrollment_id);


--
-- Name: collection_operations collection_operation_idempotency; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_idempotency UNIQUE (session_id, idempotency_key);


--
-- Name: collection_operations collection_operation_nonce; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_nonce UNIQUE (session_id, nonce);


--
-- Name: collection_operations collection_operation_ordinal; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_ordinal UNIQUE (session_id, epoch, ordinal);


--
-- Name: collection_operations collection_operations_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operations_pkey PRIMARY KEY (session_id, operation_id);


--
-- Name: collection_retention_refs collection_retention_refs_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_refs_pkey PRIMARY KEY (session_id, reference_id);


--
-- Name: collection_sessions collection_session_idempotency; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_sessions
    ADD CONSTRAINT collection_session_idempotency UNIQUE (principal_id, idempotency_key);


--
-- Name: collection_sessions collection_sessions_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_sessions
    ADD CONSTRAINT collection_sessions_pkey PRIMARY KEY (id);


--
-- Name: execution_authority execution_authority_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.execution_authority
    ADD CONSTRAINT execution_authority_pkey PRIMARY KEY (job_id);


--
-- Name: grant_runtime_state grant_runtime_state_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.grant_runtime_state
    ADD CONSTRAINT grant_runtime_state_pkey PRIMARY KEY (id);


--
-- Name: helper_enrollments helper_enrollments_certificate_digest_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.helper_enrollments
    ADD CONSTRAINT helper_enrollments_certificate_digest_key UNIQUE (certificate_digest);


--
-- Name: helper_enrollments helper_enrollments_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.helper_enrollments
    ADD CONSTRAINT helper_enrollments_pkey PRIMARY KEY (id);


--
-- Name: helper_sessions helper_sessions_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.helper_sessions
    ADD CONSTRAINT helper_sessions_pkey PRIMARY KEY (id);


--
-- Name: inference_requests inference_requests_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.inference_requests
    ADD CONSTRAINT inference_requests_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_idempotency_key_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.jobs
    ADD CONSTRAINT jobs_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: platform_metadata platform_metadata_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.platform_metadata
    ADD CONSTRAINT platform_metadata_pkey PRIMARY KEY (key);


--
-- Name: program_budget_scopes program_budget_scopes_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_budget_scopes
    ADD CONSTRAINT program_budget_scopes_pkey PRIMARY KEY (id);


--
-- Name: program_budget_subjects program_budget_subjects_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_budget_subjects
    ADD CONSTRAINT program_budget_subjects_pkey PRIMARY KEY (scope_id, subject_id);


--
-- Name: program_reservations program_reservation_subject_call; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_reservations
    ADD CONSTRAINT program_reservation_subject_call UNIQUE (scope_id, call_id, subject_id);


--
-- Name: program_reservations program_reservation_subject_effect; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_reservations
    ADD CONSTRAINT program_reservation_subject_effect UNIQUE (scope_id, subject_id, effect_key);


--
-- Name: program_reservations program_reservations_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_reservations
    ADD CONSTRAINT program_reservations_pkey PRIMARY KEY (scope_id, call_id);


--
-- Name: program_slots program_slot_call_unique; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_slots
    ADD CONSTRAINT program_slot_call_unique UNIQUE (scope_id, call_id);


--
-- Name: program_slots program_slots_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_slots
    ADD CONSTRAINT program_slots_pkey PRIMARY KEY (scope_id, slot_id);


--
-- Name: program_budget_subjects program_subject_purpose_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_budget_subjects
    ADD CONSTRAINT program_subject_purpose_key UNIQUE (scope_id, subject_id, purpose);


--
-- Name: qualifications qualifications_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.qualifications
    ADD CONSTRAINT qualifications_pkey PRIMARY KEY (id);


--
-- Name: resource_gateway_enrollments resource_gateway_enrollments_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.resource_gateway_enrollments
    ADD CONSTRAINT resource_gateway_enrollments_pkey PRIMARY KEY (id);


--
-- Name: resource_gateway_enrollments resource_gateway_enrollments_token_digest_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.resource_gateway_enrollments
    ADD CONSTRAINT resource_gateway_enrollments_token_digest_key UNIQUE (token_digest);


--
-- Name: resource_output_grants resource_output_grants_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.resource_output_grants
    ADD CONSTRAINT resource_output_grants_pkey PRIMARY KEY (id);


--
-- Name: resource_owner_requests resource_owner_requests_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.resource_owner_requests
    ADD CONSTRAINT resource_owner_requests_pkey PRIMARY KEY (id);


--
-- Name: resource_workload_scopes resource_workload_scopes_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.resource_workload_scopes
    ADD CONSTRAINT resource_workload_scopes_pkey PRIMARY KEY (workload_id);


--
-- Name: setup_drafts setup_drafts_idempotency_key_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.setup_drafts
    ADD CONSTRAINT setup_drafts_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: setup_drafts setup_drafts_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.setup_drafts
    ADD CONSTRAINT setup_drafts_pkey PRIMARY KEY (id);


--
-- Name: setup_principals setup_principals_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.setup_principals
    ADD CONSTRAINT setup_principals_pkey PRIMARY KEY (id);


--
-- Name: setup_principals setup_principals_token_hash_key; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.setup_principals
    ADD CONSTRAINT setup_principals_token_hash_key UNIQUE (token_hash);


--
-- Name: worker_execution_leases worker_execution_leases_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.worker_execution_leases
    ADD CONSTRAINT worker_execution_leases_pkey PRIMARY KEY (job_id);


--
-- Name: worker_execution_operations worker_execution_operations_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.worker_execution_operations
    ADD CONSTRAINT worker_execution_operations_pkey PRIMARY KEY (workload_id, instance_id, operation_id);


--
-- Name: worker_heartbeats worker_heartbeats_pkey; Type: CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.worker_heartbeats
    ADD CONSTRAINT worker_heartbeats_pkey PRIMARY KEY (id);


--
-- Name: ix_auth_sessions_expires_at; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_auth_sessions_expires_at ON __LEDGER_V8_SCHEMA__.auth_sessions USING btree (expires_at);


--
-- Name: ix_collection_acknowledgements_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_acknowledgements_owner ON __LEDGER_V8_SCHEMA__.collection_acknowledgements USING btree (session_id, operation_id);


--
-- Name: ix_collection_checkpoints_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_checkpoints_owner ON __LEDGER_V8_SCHEMA__.collection_checkpoints USING btree (retain_until_ms);


--
-- Name: ix_collection_enrollments_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_enrollments_owner ON __LEDGER_V8_SCHEMA__.collection_enrollments USING btree (session_id, retain_until_ms);


--
-- Name: ix_collection_epochs_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_epochs_owner ON __LEDGER_V8_SCHEMA__.collection_epochs USING btree (session_id, state);


--
-- Name: ix_collection_observations_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_observations_owner ON __LEDGER_V8_SCHEMA__.collection_observations USING btree (retain_until_ms);


--
-- Name: ix_collection_operations_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_operations_owner ON __LEDGER_V8_SCHEMA__.collection_operations USING btree (session_id, state);


--
-- Name: ix_collection_retention_refs_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_retention_refs_owner ON __LEDGER_V8_SCHEMA__.collection_retention_refs USING btree (hold, retain_until_ms);


--
-- Name: ix_collection_sessions_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_collection_sessions_owner ON __LEDGER_V8_SCHEMA__.collection_sessions USING btree (state, retain_until_ms);


--
-- Name: ix_helper_sessions_binding_id; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_helper_sessions_binding_id ON __LEDGER_V8_SCHEMA__.helper_sessions USING btree (binding_id);


--
-- Name: ix_helper_sessions_retention; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_helper_sessions_retention ON __LEDGER_V8_SCHEMA__.helper_sessions USING btree (active, last_seen, id);


--
-- Name: ix_inference_requests_binding_id; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_inference_requests_binding_id ON __LEDGER_V8_SCHEMA__.inference_requests USING btree (binding_id);


--
-- Name: ix_inference_requests_job_id; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_inference_requests_job_id ON __LEDGER_V8_SCHEMA__.inference_requests USING btree (job_id);


--
-- Name: ix_inference_requests_retention; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_inference_requests_retention ON __LEDGER_V8_SCHEMA__.inference_requests USING btree (status, updated_at, id);


--
-- Name: ix_inference_requests_session_id; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_inference_requests_session_id ON __LEDGER_V8_SCHEMA__.inference_requests USING btree (session_id);


--
-- Name: ix_jobs_caller; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_jobs_caller ON __LEDGER_V8_SCHEMA__.jobs USING btree (caller);


--
-- Name: ix_jobs_grant_id; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_jobs_grant_id ON __LEDGER_V8_SCHEMA__.jobs USING btree (grant_id);


--
-- Name: ix_jobs_receipt; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE UNIQUE INDEX ix_jobs_receipt ON __LEDGER_V8_SCHEMA__.jobs USING btree (receipt);


--
-- Name: ix_jobs_status; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_jobs_status ON __LEDGER_V8_SCHEMA__.jobs USING btree (status);


--
-- Name: ix_program_reservations_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_program_reservations_owner ON __LEDGER_V8_SCHEMA__.program_reservations USING btree (scope_id, state);


--
-- Name: ix_program_slots_owner; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_program_slots_owner ON __LEDGER_V8_SCHEMA__.program_slots USING btree (scope_id, state);


--
-- Name: ix_qualifications_generation; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_qualifications_generation ON __LEDGER_V8_SCHEMA__.qualifications USING btree (generation);


--
-- Name: ix_resource_owner_requests_job_id; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_resource_owner_requests_job_id ON __LEDGER_V8_SCHEMA__.resource_owner_requests USING btree (job_id);


--
-- Name: ix_setup_drafts_author; Type: INDEX; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

CREATE INDEX ix_setup_drafts_author ON __LEDGER_V8_SCHEMA__.setup_drafts USING btree (author);


--
-- Name: collection_acknowledgements collection_ack_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_acknowledgements
    ADD CONSTRAINT collection_ack_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_acknowledgements collection_ack_enrollment_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_acknowledgements
    ADD CONSTRAINT collection_ack_enrollment_fk FOREIGN KEY (enrollment_id, session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_enrollments(id, session_id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_acknowledgements collection_ack_operation_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_acknowledgements
    ADD CONSTRAINT collection_ack_operation_fk FOREIGN KEY (session_id, operation_id, enrollment_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_operations(session_id, operation_id, enrollment_id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_checkpoints collection_checkpoint_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_checkpoints
    ADD CONSTRAINT collection_checkpoint_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_checkpoints collection_checkpoint_session_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_checkpoints
    ADD CONSTRAINT collection_checkpoint_session_fk FOREIGN KEY (session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_sessions(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_enrollments collection_enrollment_epoch_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_enrollments
    ADD CONSTRAINT collection_enrollment_epoch_fk FOREIGN KEY (session_id, original_epoch) REFERENCES __LEDGER_V8_SCHEMA__.collection_epochs(session_id, epoch) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_epochs collection_epoch_session_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_epochs
    ADD CONSTRAINT collection_epoch_session_fk FOREIGN KEY (session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_sessions(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_epoch_guards collection_guard_epoch_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_epoch_guards
    ADD CONSTRAINT collection_guard_epoch_fk FOREIGN KEY (session_id, active_epoch) REFERENCES __LEDGER_V8_SCHEMA__.collection_epochs(session_id, epoch) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_epoch_guards collection_guard_session_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_epoch_guards
    ADD CONSTRAINT collection_guard_session_fk FOREIGN KEY (session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_sessions(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_observations collection_observation_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_observations
    ADD CONSTRAINT collection_observation_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_observations collection_observation_session_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_observations
    ADD CONSTRAINT collection_observation_session_fk FOREIGN KEY (session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_sessions(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_operations collection_operation_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_operations collection_operation_enrollment_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_enrollment_fk FOREIGN KEY (enrollment_id, session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_enrollments(id, session_id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_operations collection_operation_epoch_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_operations
    ADD CONSTRAINT collection_operation_epoch_fk FOREIGN KEY (session_id, epoch, epoch_purpose) REFERENCES __LEDGER_V8_SCHEMA__.collection_epochs(session_id, epoch, epoch_purpose) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_retention_refs collection_retention_ack_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_ack_fk FOREIGN KEY (ack_enrollment_id, ack_sequence, session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_acknowledgements(enrollment_id, sequence, session_id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_retention_refs collection_retention_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_retention_refs collection_retention_checkpoint_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_checkpoint_fk FOREIGN KEY (session_id, checkpoint_sequence) REFERENCES __LEDGER_V8_SCHEMA__.collection_checkpoints(session_id, sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_retention_refs collection_retention_observation_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_observation_fk FOREIGN KEY (session_id, observation_sequence) REFERENCES __LEDGER_V8_SCHEMA__.collection_observations(session_id, sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_retention_refs collection_retention_qualification_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_qualification_fk FOREIGN KEY (qualification_id) REFERENCES __LEDGER_V8_SCHEMA__.qualifications(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_retention_refs collection_retention_session_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_retention_refs
    ADD CONSTRAINT collection_retention_session_fk FOREIGN KEY (session_id) REFERENCES __LEDGER_V8_SCHEMA__.collection_sessions(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: collection_sessions collection_session_scope_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.collection_sessions
    ADD CONSTRAINT collection_session_scope_fk FOREIGN KEY (scope_id) REFERENCES __LEDGER_V8_SCHEMA__.program_budget_scopes(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: program_reservations program_reservation_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_reservations
    ADD CONSTRAINT program_reservation_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: program_reservations program_reservation_subject_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_reservations
    ADD CONSTRAINT program_reservation_subject_fk FOREIGN KEY (scope_id, subject_id, purpose) REFERENCES __LEDGER_V8_SCHEMA__.program_budget_subjects(scope_id, subject_id, purpose) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: program_slots program_slot_audit_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_slots
    ADD CONSTRAINT program_slot_audit_fk FOREIGN KEY (audit_sequence) REFERENCES __LEDGER_V8_SCHEMA__.audit_events(sequence) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: program_slots program_slot_reservation_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_slots
    ADD CONSTRAINT program_slot_reservation_fk FOREIGN KEY (scope_id, call_id, subject_id) REFERENCES __LEDGER_V8_SCHEMA__.program_reservations(scope_id, call_id, subject_id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: program_slots program_slot_subject_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_slots
    ADD CONSTRAINT program_slot_subject_fk FOREIGN KEY (scope_id, subject_id) REFERENCES __LEDGER_V8_SCHEMA__.program_budget_subjects(scope_id, subject_id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- Name: program_budget_subjects program_subject_scope_fk; Type: FK CONSTRAINT; Schema: __LEDGER_V8_SCHEMA__; Owner: -
--

ALTER TABLE ONLY __LEDGER_V8_SCHEMA__.program_budget_subjects
    ADD CONSTRAINT program_subject_scope_fk FOREIGN KEY (scope_id) REFERENCES __LEDGER_V8_SCHEMA__.program_budget_scopes(id) ON UPDATE RESTRICT ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--
