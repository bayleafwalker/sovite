import "@typespec/http";
import "@typespec/json-schema";
import "@vuoro/contracts";
using TypeSpec.Http;
using TypeSpec.JsonSchema;
using Vuoro.Contracts;
using Vuoro.Contracts.Http;
@service(#{ title: "Sovite Repository Repair v1" })
@jsonSchema
namespace Sovite.RepositoryRepair.V1;
@minLength(1) @maxLength(128) @pattern("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
scalar Identity extends string;
@minLength(64) @maxLength(64) @pattern("^[0-9a-f]{64}$")
scalar Sha256 extends string;
@minLength(72) @maxLength(72) @pattern("^release_[0-9a-f]{64}$")
scalar OutputRelease extends string;
model RepairFeedback {
  schema_version: "sovite-repository-repair-feedback/v1";
  parent_run_id: Identity;
  candidate_export_sha256: Sha256;
  @minLength(40) @maxLength(40) @pattern("^[0-9a-f]{40}$") candidate_tip_sha: string;
  result: "check_failed" | "review_failed";
  @minLength(1) @maxLength(48000) feedback: string;
}
model RepairSubmission {
  schema_version: "sovite-repository-repair/v1";
  parent_run_id: Identity;
  candidate_sha256: Sha256;
  feedback_release_id: OutputRelease;
  preview_id: Sha256;
  @minValue(0) @maxValue(100000) max_cost_usd: float64;
}
model RepairAccepted {
  run_id: Identity;
  parent_run_id: Identity;
  root_run_id: Identity;
  @minValue(1) @maxValue(3) repair_number: int32;
}
@minLength(71) @maxLength(71) @pattern("^sha256:[0-9a-f]{64}$")
scalar DigestRef extends string;
model FeedbackContextRequest {
  parent_run_id: Identity;
  feedback_contract_id: Identity;
}
model FeedbackContext {
  parent_run_id: Identity;
  source_digest: DigestRef;
  contract_id: Identity;
  contract_revision: DigestRef;
  expected_generation: DigestRef;
  candidate_sha256: Sha256;
  @minLength(40) @maxLength(40) @pattern("^[0-9a-f]{40}$") candidate_tip_sha: string;
}
model ErrorDetail {
  @minLength(1) @maxLength(96) code: string;
  @minLength(1) @maxLength(512) message: string;
}
model ErrorEnvelope { detail: ErrorDetail; }
@error model Error401 is JsonResponse<401, ErrorEnvelope>;
@error model Error403 is JsonResponse<403, ErrorEnvelope>;
@error model Error404 is JsonResponse<404, ErrorEnvelope>;
@error model Error409 is JsonResponse<409, ErrorEnvelope>;
@error model Error413 is JsonResponse<413, ErrorEnvelope>;
@error model Error422 is JsonResponse<422, ErrorEnvelope>;
@error model Error503 is JsonResponse<503, ErrorEnvelope>;
@route("/api/repository-runs/v1/repairs") @post
op repositoryRepairSubmit(@body body: RepairSubmission): JsonResponse<201, RepairAccepted>
  | Error401 | Error403 | Error404 | Error409 | Error413 | Error422 | Error503;

@route("/api/repository-runs/v1/repair-feedback-context") @post
op repositoryRepairFeedbackContext(@body body: FeedbackContextRequest): JsonResponse<200, FeedbackContext>
  | Error401 | Error403 | Error404 | Error409 | Error413 | Error422 | Error503;
