import "@typespec/http";
import "@typespec/json-schema";
import "@vuoro/contracts";
using TypeSpec.Http;
using TypeSpec.JsonSchema;
using Vuoro.Contracts;
using Vuoro.Contracts.Http;
@service(#{ title: "Sovite closed resource owner v2" })
@jsonSchema
namespace Sovite.ResourceOwner.V2;

@minLength(1) @maxLength(128) @pattern("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
scalar Identity extends string;
@minLength(8) @maxLength(4096) scalar Bearer extends string;
@minLength(1) @maxLength(160) scalar Principal extends string;
@minLength(1) @maxLength(64) @pattern("^[A-Za-z0-9_-]+$") scalar OperationId extends string;
@minLength(64) @maxLength(64) @pattern("^[0-9a-f]{64}$") scalar Sha256 extends string;
@minLength(71) @maxLength(71) @pattern("^sha256:[0-9a-f]{64}$") scalar Digest extends string;
@minLength(32) @maxLength(32) @pattern("^[0-9a-f]{32}$") scalar Fence extends string;
@minValue(0) @maxValue(1000000000000) scalar Epoch extends float64;

model C0Profile { source_class: "C0"; output_class: "C0"; destination: Identity; }
model C1Profile { source_class: "C1"; output_class: "C1"; destination: Identity; }
model C2Profile { source_class: "C2"; output_class: "C2"; destination: Identity; }
union DataProfile { c0: C0Profile, c1: C1Profile, c2: C2Profile }
model PeerHealthEntry {
  kind: "model-peer-health";
  target_id: Identity;
  target_revision: Digest;
  read_profile: DataProfile;
}
model LabEntry {
  kind: "kvm-lab";
  target_id: Identity;
  target_revision: Digest;
  create_profile: DataProfile;
  observe_profile: DataProfile;
  stop_profile: DataProfile;
}
union ResourceEntry { health: PeerHealthEntry, lab: LabEntry }
model TargetCatalog {
  schema_version: "sovite-resource-catalog/v2";
  @minItems(1) @maxItems(2) entries: ResourceEntry[];
}
model WorkloadScope {
  workload_id: Principal;
  gateway_id: Identity;
  tenant_id: Identity;
  workspace_id: Identity;
  domain_id: Identity;
  catalog: TargetCatalog;
}
model ScopeInstalled { workload_id: Principal; updated: true; }

union ResourceOperation {
  health: "model-peer.health-read",
  create: "kvm-lab.create",
  observe: "kvm-lab.observe",
  stop: "kvm-lab.stop",
}
model OutputGrantRequest {
  workload_id: Principal;
  principal_id: Principal;
  tenant_id: Identity;
  workspace_id: Identity;
  domain_id: Identity;
  target_id: Identity;
  operation: ResourceOperation;
  policy_revision: Digest;
}
model OutputGrantInstalled {
  grant_id: Identity;
  schema_version: "sovite-resource-peer-health/v1" | "sovite-resource-lab-created/v1" | "sovite-resource-lab-observed/v1" | "sovite-resource-lab-stopped/v1";
  destination: Identity;
  policy_revision: Digest;
}

model ExecutionContext {
  job_id: OperationId;
  instance_id: OperationId;
  generation: Digest;
  fence: Fence;
  operation_id: OperationId;
}
model HealthSelector { operation: "model-peer.health-read"; target_id: Identity; }
model LabCreateSelector { operation: "kvm-lab.create"; target_id: Identity; }
model LabObserveSelector { operation: "kvm-lab.observe"; target_id: Identity; create_request_id: Sha256; }
model LabStopSelector { operation: "kvm-lab.stop"; target_id: Identity; create_request_id: Sha256; }
union ResourceSelector {
  health: HealthSelector,
  create: LabCreateSelector,
  observe: LabObserveSelector,
  stop: LabStopSelector,
}
model SubmitRequest { execution: ExecutionContext; resource: ResourceSelector; }
model Submitted { request_id: Sha256; state: "pending" | "observed"; fence_fingerprint: Digest; }
model ResourceRequest {
  schema_version: "sovite-resource-request/v2";
  workspace_id: Identity;
  resource_domain_id: Identity;
  resource: ResourceSelector;
  target_revision: Digest;
  execution_deadline: Epoch;
}
model CreateBinding {
  create_request_id: Sha256;
  principal_id: Principal;
  gateway_id: Identity;
  tenant_id: Identity;
  workspace_id: Identity;
  domain_id: Identity;
  target_id: Identity;
  target_revision: Digest;
  created_at: Epoch;
  execution_deadline: Epoch;
}
model GatewayRequest {
  request_id: Sha256;
  job_id: OperationId;
  principal_id: Principal;
  audience: "resource-gateway";
  instance_id: OperationId;
  generation: Digest;
  gateway_id: Identity;
  tenant_id: Identity;
  workspace_id: Identity;
  domain_id: Identity;
  workload: Principal;
  fence_fingerprint: Digest;
  request: ResourceRequest;
  target: ResourceEntry;
  create?: CreateBinding;
  created_at: Epoch;
  deadline: Epoch;
}
model PollRequest { after?: Sha256 | null; }
model PollResult { @maxItems(100) requests: GatewayRequest[]; next_cursor: Sha256 | null; }
model CurrentRequest {
  request_id: Sha256;
  fence_fingerprint: Digest;
  policy_revision: Digest;
  destination: Identity;
  actor: Principal;
}
model CurrentResult { current: boolean; }
model DeliveryRequest {
  ...CurrentRequest;
  observation_digest: Digest;
  @maxLength(6000) observation_b64: string;
  release: "allowed" | "denied";
}
model Delivered { accepted: boolean; state: "stored" | "conflict" | "withheld" | "fenced"; }
model ResultReadRequest { execution: ExecutionContext; request_id: Sha256; }

model PeerHealthObservation {
  schema_version: "sovite-resource-peer-health/v1";
  workspace_id: Identity;
  resource_domain_id: Identity;
  target_id: Identity;
  operation: "model-peer.health-read";
  health_scope: "peer-http-daemon";
  daemon_responsive: true;
}
model LabObservationFields {
  workspace_id: Identity;
  resource_domain_id: Identity;
  target_id: Identity;
  lab_id: Sha256;
  state: "running" | "stopped" | "absent" | "unknown";
  effect_outcome: "known" | "unknown";
  deadline: Epoch;
}
model LabCreated {
  ...LabObservationFields;
  schema_version: "sovite-resource-lab-created/v1";
  operation: "kvm-lab.create";
}
model LabObserved {
  ...LabObservationFields;
  schema_version: "sovite-resource-lab-observed/v1";
  operation: "kvm-lab.observe";
}
model LabStopped {
  ...LabObservationFields;
  schema_version: "sovite-resource-lab-stopped/v1";
  operation: "kvm-lab.stop";
}
union Observation { health: PeerHealthObservation, create: LabCreated, observe: LabObserved, stop: LabStopped }
model ResourceResult {
  observation: Observation;
  sha256: Digest;
  output_class: "C0" | "C1" | "C2";
  destination: Identity;
  policy_revision: Digest;
}

model GatewayPolicy {
  schema_version: "sovite-resource-policy/v2";
  gateway_id: Identity;
  audience: "resource-gateway";
  tenant_id: Identity;
  principal_id: Principal;
  workspace_id: Identity;
  domain_id: Identity;
  expires_at: Epoch;
  catalog: TargetCatalog;
  release_policy_revision: Digest;
}
model PeerHealthSource { error: PeerHealthMessage; }
model PeerHealthMessage { message: "ok"; }
model PeerHealthConfiguration {
  kind: "model-peer-health";
  target_id: Identity;
  @minLength(1) @maxLength(512) origin: string;
  method: "GET";
  path: "/healthz";
  @minLength(1) @maxLength(4096) ca_file: string;
  ca_sha256: Sha256;
}
model LabConfiguration {
  kind: "kvm-lab";
  target_id: Identity;
  template_id: "diskless-kvm-lab-v1";
  template_sha256: Sha256;
  @minValue(1) @maxValue(300) max_ttl_seconds: int32;
}
union TargetConfiguration { health: PeerHealthConfiguration, lab: LabConfiguration }
model InstalledCatalog {
  schema_version: "sovite-resource-installed-catalog/v2";
  @minItems(1) @maxItems(2) entries: TargetConfiguration[];
}
model LabHostRequest {
  schema_version: "cloud.kvm-lab-control.v1";
  verb: "create" | "observe" | "stop";
  operation_id: OperationId;
  create_operation_id: OperationId;
  template_id: "diskless-kvm-lab-v1";
  template_sha256: Sha256;
  deadline: Epoch;
}
@minLength(36) @maxLength(36) @pattern("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
scalar BootId extends string;
model LabHostObservation {
  schema_version: "cloud.kvm-lab-observation.v1";
  operation_id: OperationId;
  create_operation_id: OperationId;
  template_id: "diskless-kvm-lab-v1";
  template_sha256: Sha256;
  deadline: Epoch;
  state: "running" | "stopped" | "absent" | "unknown";
  host_boot_id: BootId;
  created_boot_id: BootId | null;
  @minLength(1) @maxLength(128) unit: string;
  invocation_id: Fence | null;
}

model ErrorDetail { @minLength(1) @maxLength(96) code: string; @minLength(1) @maxLength(512) message: string; }
model ErrorEnvelope { detail: ErrorDetail; }
@error model Error400 is JsonResponse<400, ErrorEnvelope>;
@error model Error403 is JsonResponse<403, ErrorEnvelope>;
@error model Error404 is JsonResponse<404, ErrorEnvelope>;
@error model Error409 is JsonResponse<409, ErrorEnvelope>;
@error model Error413 is JsonResponse<413, ErrorEnvelope>;
@error model Error422 is JsonResponse<422, ErrorEnvelope>;
@error model Error503 is JsonResponse<503, ErrorEnvelope>;

@route("/internal/resource-owner/v2/workload-scopes") @put
op resourceScopeInstall(@header("Authorization") authorization: Bearer, @body body: WorkloadScope): JsonResponse<200, ScopeInstalled> | Error400 | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/resource-owner/v2/output-grants") @post
op resourceOutputGrant(@header("Authorization") authorization: Bearer, @body body: OutputGrantRequest): JsonResponse<200, OutputGrantInstalled> | Error400 | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/resource-owner/v2/requests") @post
op resourceSubmit(@header("Authorization") authorization: Bearer, @body body: SubmitRequest): JsonResponse<200, Submitted> | Error400 | Error403 | Error404 | Error409 | Error413 | Error422 | Error503;
@route("/internal/resource-owner/v2/poll") @post
op resourcePoll(@header("Authorization") authorization: Bearer, @body body: PollRequest): JsonResponse<200, PollResult> | Error400 | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/resource-owner/v2/current") @post
op resourceCurrent(@header("Authorization") authorization: Bearer, @body body: CurrentRequest): JsonResponse<200, CurrentResult> | Error400 | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/resource-owner/v2/result") @post
op resourceResult(@header("Authorization") authorization: Bearer, @body body: DeliveryRequest): JsonResponse<200, Delivered> | Error400 | Error403 | Error409 | Error413 | Error422 | Error503;
@route("/internal/resource-owner/v2/result/read") @post
op resourceResultRead(@header("Authorization") authorization: Bearer, @body body: ResultReadRequest): JsonResponse<200, ResourceResult> | Error400 | Error403 | Error404 | Error409 | Error413 | Error422 | Error503;
