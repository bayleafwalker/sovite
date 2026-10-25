import "@typespec/http";
import "@typespec/json-schema";
import "@vuoro/contracts";
using TypeSpec.Http;
using TypeSpec.JsonSchema;
using Vuoro.Contracts;
using Vuoro.Contracts.Http;

@service(#{ title: "Sovite Execution API v1" })
@jsonSchema
namespace Sovite.Execution.V1;

@minLength(1) @maxLength(64) @pattern("^[A-Za-z0-9_-]+$")
scalar InstanceId extends string;
@minLength(1) @maxLength(64) @pattern("^[A-Za-z0-9_-]+$")
scalar OperationId extends string;
@minLength(1) @maxLength(64) @pattern("^[A-Za-z0-9_-]+$")
scalar JobId extends string;
@minLength(71) @maxLength(71) @pattern("^sha256:[0-9a-f]{64}$")
scalar Generation extends string;
@minLength(32) @maxLength(32) @pattern("^[0-9a-f]{32}$")
scalar Fence extends string;
@minValue(0.0000000000000001)
scalar UnixSeconds extends float64;

model PullRequest { instance_id: InstanceId; operation_id: OperationId; }
model ExecutionIdentity {
  instance_id: InstanceId;
  job_id: JobId;
  generation: Generation;
  fence: Fence;
}
model StartRequest { instance_id: InstanceId; job_id: JobId; generation: Generation; fence: Fence; operation_id: OperationId; }
model RenewRequest { instance_id: InstanceId; job_id: JobId; generation: Generation; fence: Fence; operation_id: OperationId; }
model StatusRequest { instance_id: InstanceId; job_id: JobId; generation: Generation; fence: Fence; }
model HandbackRequest { instance_id: InstanceId; job_id: JobId; generation: Generation; fence: Fence; operation_id: OperationId; }
model LookupRequest { instance_id: InstanceId; operation_id: OperationId; }

model AgentHttpRecipe {
  profile: "agent-http-v1";
  @minLength(1) @maxLength(96) agent_id: string;
  @minLength(1) @maxLength(16000) endpoint: string;
  @minLength(1) @maxLength(16000) cancel_endpoint?: string;
  @minLength(2) @maxLength(96) @pattern("^[A-Z][A-Z0-9_]+$") credential_env?: string;
  idempotent: boolean;
  @minValue(1) @maxValue(2147483647) timeout_seconds: int32;
}
model ReservedJob {
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  lease_until: UnixSeconds;
  @maxLength(3500000) task: string;
  recipe: AgentHttpRecipe;
}
model PullIdle { state: "idle"; }
model PullReserved { state: "reserved"; job: ReservedJob; }
union PullResult { idle: PullIdle, reserved: PullReserved }

model Started {
  state: "started";
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  lease_until: UnixSeconds;
  deadline: UnixSeconds;
}
model AlreadyStarted {
  state: "already_started";
  job_id: JobId;
  generation: Generation;
  fence: Fence;
}
union StartResult { started: Started, alreadyStarted: AlreadyStarted }

model Renewed {
  state: "renewed";
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  lease_until: UnixSeconds;
  deadline: UnixSeconds;
}
model ExecutionStatus {
  state: "current" | "superseded" | "finished" | "expired";
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  phase: "reserved" | "started" | "finished" | null;
  job_status: "queued" | "running" | "retry_wait" | "completed" | "failed" | "cancelled" | "blocked" | null;
  lease_until: UnixSeconds | null;
  deadline: UnixSeconds | null;
}
model HandedBack {
  state: "handed_back";
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  disposition: "retry_wait" | "blocked" | "cancelled";
}
model Completion {
  state: "completed" | "failed" | "cancelled" | "retry_wait" | "blocked";
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  error_code?: string | null;
  artifact_sha256?: string;
}
model OperationFound {
  state: "found";
  operation_id: OperationId;
  operation_kind: "pull" | "start" | "renew" | "handback" | "complete";
  job_id: JobId;
  fence: Fence;
  result_state: "idle" | "reserved" | "started" | "already_started" | "renewed" | "handed_back" | "completed" | "failed" | "cancelled" | "retry_wait" | "blocked";
}

union WorkerOutcome { completed: "completed", failed: "failed", retryable: "retryable_failure", cancelled: "cancelled" }
model CompleteRequest {
  instance_id: InstanceId;
  job_id: JobId;
  generation: Generation;
  fence: Fence;
  operation_id: OperationId;
  outcome: WorkerOutcome;
  @maxLength(60000) result_json?: string;
  error_code?: string | null;
}

union ExecutionErrorCode {
  invalidRequest: "invalid_request",
  executionScopeDenied: "execution_scope_denied",
  executionProfileUnavailable: "execution_profile_unavailable",
  idempotencyConflict: "idempotency_conflict",
  executionFenced: "execution_fenced",
  cancelled: "cancelled",
  ownerUnavailable: "owner_unavailable",
  payloadTooLarge: "execution_payload_too_large",
  notFound: "not_found",
  previousAttemptUnavailable: "previous_attempt_unavailable",
  previousAttemptUnfinished: "previous_attempt_unfinished",
  previousEffectUncertain: "previous_effect_uncertain",
  adapterCredentialMissing: "adapter_credential_missing",
  agentEndpointMissing: "agent_endpoint_missing",
  authorizationRevoked: "authorization_revoked",
  executionFailed: "execution_failed",
  executionTimeout: "execution_timeout",
  externalExecutionUncertain: "external_execution_uncertain",
  incompatibleDemoConfiguration: "incompatible_demo_configuration",
  internalExecutionError: "internal_execution_error",
  invalidToolCalls: "invalid_tool_calls",
  modelEmptyResult: "model_empty_result",
  modelGatewayAdapterUnavailable: "model_gateway_adapter_unavailable",
  modelIdentityMissing: "model_identity_missing",
  modelIdentityMismatch: "model_identity_mismatch",
  modelIdentityProtocolUnsupported: "model_identity_protocol_unsupported",
  modelIdentityUnpinned: "model_identity_unpinned",
  modelOutputFiltered: "model_output_filtered",
  modelOutputTruncated: "model_output_truncated",
  modelProtocolError: "model_protocol_error",
  modelTrustMissing: "model_trust_missing",
  nativeEmptyResult: "native_empty_result",
  nativeIncompleteResult: "native_incomplete_result",
  nativeProcessRequiresExternalIsolation: "native_process_requires_external_isolation",
  nativeProviderMismatch: "native_provider_mismatch",
  nativeProtocolError: "native_protocol_error",
  nativeResultMissing: "native_result_missing",
  nativeResultTooLarge: "native_result_too_large",
  nativeRuntimeFailed: "native_runtime_failed",
  nativeRuntimeUnavailable: "native_runtime_unavailable",
  nativeToolsUnsupported: "native_tools_unsupported",
  stepLimitReached: "step_limit_reached",
  subscriptionCredentialChanged: "subscription_credential_changed",
  subscriptionCredentialInvalid: "subscription_credential_invalid",
  subscriptionCredentialMissing: "subscription_credential_missing",
  subscriptionCredentialUnavailable: "subscription_credential_unavailable",
  subscriptionProfileBusy: "subscription_profile_busy",
  toolAgentRequiresChatApi: "tool_agent_requires_chat_api",
  toolCredentialMissing: "tool_credential_missing",
  toolOperationDenied: "tool_operation_denied",
  unsupportedHarness: "unsupported_harness",
  unsupportedModelAdapter: "unsupported_model_adapter",
  unsupportedTool: "unsupported_tool",
  upstreamProtocolError: "upstream_protocol_error",
  upstreamRejected: "upstream_rejected",
  upstreamResponseTooLarge: "upstream_response_too_large",
  upstreamUnavailable: "upstream_unavailable",
}
model ErrorDetail { code: ExecutionErrorCode; message: string; }
model ErrorEnvelope { detail: ErrorDetail; }
@error model Error403 is JsonResponse<403, ErrorEnvelope>;
@error model Error404 is JsonResponse<404, ErrorEnvelope>;
@error model Error409 is JsonResponse<409, ErrorEnvelope>;
@error model Error413 is JsonResponse<413, ErrorEnvelope>;
@error model Error422 is JsonResponse<422, ErrorEnvelope>;
@error model Error503 is JsonResponse<503, ErrorEnvelope>;

@route("/internal/execution/v1/pull") @post
op executionPull(@body body: PullRequest): JsonResponse<200, PullResult> | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/execution/v1/start") @post
op executionStart(@body body: StartRequest): JsonResponse<200, StartResult> | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/execution/v1/renew") @post
op executionRenew(@body body: RenewRequest): JsonResponse<200, Renewed> | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/execution/v1/status") @post
op executionStatus(@body body: StatusRequest): JsonResponse<200, ExecutionStatus> | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/execution/v1/handback") @post
op executionHandback(@body body: HandbackRequest): JsonResponse<200, HandedBack> | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/execution/v1/complete") @post
op executionComplete(@body body: CompleteRequest): JsonResponse<200, Completion> | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/execution/v1/lookup") @post
op executionLookup(@body body: LookupRequest): JsonResponse<200, OperationFound> | Error403 | Error404 | Error413 | Error422 | Error503;
