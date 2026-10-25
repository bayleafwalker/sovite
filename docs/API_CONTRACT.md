# Console API contract

Operator endpoints require authorization via `Authorization: Bearer …` or the HttpOnly `sovite_session` cookie. Workflow definition/binding endpoints use author authority; workflow job and approved-output endpoints have the scoped authorities described below. Cookie mutations require an exact `Origin` matching `SOVITE_PUBLIC_ORIGIN`. Requests and responses are JSON. The static console and API use the same origin. No operator credential is stored in browser localStorage. Errors use `{detail:{code,message}}`; messages never contain model or tool output.

Hosted installations only (a `SOVITE_HOSTING_ENVELOPE` is configured on the service) expose the author/activator bundle routes. They are not operator routes, and the operator token is refused on both. `sovite compile` and `sovite activate` are clients of these routes: they use `SOVITE_CONTROL_URL` plus `SOVITE_AUTHOR_TOKEN` or `SOVITE_ACTIVATOR_TOKEN`, need no database credential, and are the only hosted CLI paths for compilation and activation.

- `POST /api/bundles` body `{bundle:object}` requires the author role and returns 201 `{status:"compiled",generation:string}`. It stores a draft revision and does not change the active generation. A bundle that exceeds the current policy or hosting envelope returns 422.
- `POST /api/bundles/{generation}/activate` takes no body, requires the activator role and returns 200 `{status:"activated",generation:string}` for the requested generation. Misuse returns 401/403/404/409/422. There is no hosted `reload`.

- `POST /api/auth/session` body `{token:string}` creates a cookie and returns `{identity:"operator",role:"operator"}`. `GET /api/auth/session` returns the authenticated identity. `DELETE /api/auth/session` signs out.
- `GET /api/overview` returns generation, component/job counts, worker observations, qualification state, audit verification, storage/security settings and capabilities. A catalog entry is not evidence of live model readiness.
- `GET /api/catalog` returns arrays of declared models, harnesses, agents, tools, compute, destinations, data classes and profiles plus generation.
- `GET /api/grants` returns declared grants with inhibit and qualification state. `POST /api/grants/{id}/inhibit` and `POST /api/grants/{id}/resume` accept `{reason:string}`. An inhibit blocks dispatch/release and requests cancellation; resume cannot override disabled configuration or missing qualification.
- `GET /api/jobs?limit=100` returns jobs. `POST /api/jobs` accepts `{grant_id,task,idempotency_key?,input_class?}`. Operator submission uses deliberate operator authority while retaining class, workflow-only, qualification and inhibit checks.
- `GET /api/jobs/{id}` includes the task and resolved recipe. `POST /api/jobs/{id}/cancel` requests cancellation. `GET /api/jobs/{id}/artifact` is the operator artifact path; show sensitive content only after deliberate interaction.
- Job states are `queued,running,retry_wait,completed,failed,cancelled,blocked`. A retained artifact does not by itself establish current content-release authority. Error codes are bounded values, never provider exception text.
- `GET /api/receipts` is operator-only. Caller projections do not reveal private job IDs, artifact-store URLs, provider task IDs or provider errors.
- `GET /api/audit?limit=200` returns bounded metadata and chain verification. Task, model and tool bodies are excluded.
- `GET /api/qualifications` returns signed evidence and current validity. Creation occurs through CLI, not the console.
- `GET /api/deployments` reports desired bundle revisions, not proof of completed Kubernetes reconciliation.
- `GET /health/live` returns `{"status":"ok"}`. `GET /health/ready` checks the database and artifact backend, not model competence.

## Caller MCP and result release

`POST /mcp` is an authenticated JSON-RPC endpoint for configured caller tokens; operator tokens are rejected. Allowed methods are `initialize`, `notifications/initialized`, `ping`, `tools/list` and `tools/call`. This transport uses bounded request/response JSON without server-side SSE streams.

| Tool | Input | Result authority |
| --- | --- | --- |
| `list_candidates` | `{input_class?}` | Lists caller-visible grants; details require installed discovery-class clearance. No reservation or live-account/capacity probe. |
| `begin_task` | `{grant_id,task,idempotency_key,input_class?,expected_generation?,previous_attempt?,selection_reason?}` | Admits under current authority and returns the installed projection. Lineage is metadata only; the rationale is private job data. |
| `task_status` | `{receipt}` | Rechecks ownership and current authority; returns the producing job's authorized projection. |
| `read_result` | `{receipt}` | Releases content only to an exact configured scoped reader. A producing caller uses task_status for class-aware content unless also listed in result_readers. |

A `reference` grant preserves the original two-field `{receipt,status}` projection and the fixed pending/available/unavailable status vocabulary. A `class-aware` grant may include content only when `output_class` fits the current caller clearance. A receipt alone is never a bearer read credential.

An exact identity in `result_readers` can read the producing job's artifact without operator-wide access. Reader membership is required in both the pinned and current grant, and the identity must remain configured. This is scoped content authority, not permission to submit the producer's work. Current producer authority, generation, qualification and inhibits are checked on release. Artifact digest and job storage identity are verified, with authority checked again after storage I/O. An absent legacy output declaration resolves to the highest installed class. Failure never releases content; provider errors and artifact bodies are excluded from audit metadata.

Detailed discovery uses optional installed `discovery_class`, defaulting to the highest installed class without mutating existing bundle hashes. Each candidate's `caller_clearance` must meet it. Below clearance only `grant_id`, `return_contract` and `details: clearance_required` are returned; generation and observation details are withheld. A detailed response separates declared route/capability/class/return facts from current grant-policy/installation-qualification observations. Workflow-only routes say `bound_workflow_required`. Capacity, gateway runtime availability, live accounts, quality and prices are not probed. Every admission and later owner boundary still rechecks the actual request.

`selection_reason` is optional nonempty UTF-8 text of at most 500 characters, retained only in private operator-visible `resolved.selection`, under the job's class/placement. It is absent from model input, caller status, discovery and audit details; missing rationale is explicitly `not_supplied`. `previous_attempt` is an owned receipt, at most 256 characters, recorded separately from an approved transfer. It reads no artifact and grants no content access.

An exact same-role idempotent replay recovers its existing receipt before re-admission; release still rechecks current authority. A changed binding, rationale or lineage under that caller-scoped key returns `idempotency_conflict` with the owned receipt. MCP reports `previous_attempt_unfinished` and `previous_effect_uncertain` distinctly, without private output. Unknown and foreign receipts share a generic refusal. See [the discovery and retry contract](CALLER-DISCOVERY.md).

## Data class and workflow admission

An omitted job or workflow `input_class` selects the highest-ranked installed class. An explicit class must exist and fit the selected grant and every receiving destination. Inputs at or above v1alpha2's `restricted_class`, or the highest installed class in v1alpha1, require private-boundary inference. `destinations[].boundary` is private or public; omission derives private from local/private kinds and public from cloud. Native subscription providers and OpenCode Go must use a public cloud destination. Changing a model does not declassify its input.

| Endpoint | Body | Authority and effect |
| --- | --- | --- |
| `POST /api/workflows/validate` | `{workflow}` | An author, or an operator in a self-hosted installation without a hosting envelope, validates structure. It creates no runtime authority. |
| `POST /api/workflows/bind` | `{workflow,requirements_ref,session_policy_ref,expected_generation,input_class?}` | The same author boundary selects and durably records a binding after checking every role grant, destination and transfer. |
| `POST /api/workflows/jobs` | `{binding,role,task?,idempotency_key,source_release?,transfer_id?,previous_attempt?,selection_reason?}` | A caller or operator submits a delivery role from an author-recorded current binding. Caller role-grant authority still applies. |

Bindings carry pinned requirements/session-policy references, workflow revision, installation identity, bundle generation, role selections, input classes and transfer policy. A valid hash or local compilation is not an admission credential: the exact binding must have been recorded through the author boundary. External authoring roles cannot be submitted as delivery jobs. Grants marked `workflow_only: true` reject ordinary MCP and direct operator submission. Superseded generations and inhibited source/target authority fence existing and new work.

## Approved output and declassification

An installed `output_contracts` entry names the source ceiling, output class, permitted destinations, verification procedure and authorized approvers. Class reduction and private-to-public workflow transfer require explicit output approval. The API records an approver's decision over exact bytes; a model assertion or output class label does not prove the transformation is safe.

- `POST /api/output-releases` accepts `{source_job_id,source_digest,contract_id,expected_contract_revision,expected_generation,content,evidence_refs}`. The source must be a currently authorized completed job. Its stored artifact and supplied digest are checked. The authenticated identity must be listed in the exact installed contract's approvers; non-operators must remain configured caller identities. At least one pinned evidence reference is required.
- Approval stores exact output bytes and returns `{release_id,output_class,output_digest,contract_revision,destinations}`. An identical repeated approval reuses the durable release identity. It grants no authority beyond the listed destinations and cannot revive a superseded source.
- `GET /api/output-releases/{release_id}` is restricted to the contract's approvers and returns verified approved content, class and destinations. This is separate from scoped `read_result` access.
- A role submission naming `source_release` must name the bound `transfer_id` and supply no additional task text. The server verifies source binding/role, contract revision, receiving model destination and input class, then supplies only approved content. A role requiring an output release cannot be started through an ordinary task string.

Approvers perform the configured verification procedure before approval. Digests identify the artifacts considered; pinned evidence records the decision inputs. Neither alone proves the procedure was correctly performed. Source and release authority are checked again when approved content is read or transferred.

## External native harness contract

The worker sends the following exact body to the configured agent-http service's `POST /run` endpoint:

```json
{
  "task_id": "durable-job-id",
  "agent_id": "fixed-installed-agent",
  "configuration_revision": "sha256:compiled-generation",
  "input": {"task": "Complete this bounded task."}
}
```

The request uses the configured bearer token and `Idempotency-Key: durable-job-id`. Set `SOVITE_HARNESS_AGENT_ID` and `SOVITE_BUNDLE_PATH` on the fixed service. When the platform uses a hosting envelope, load the same `SOVITE_HOSTING_ENVELOPE`; the service compiles that exact generation and rejects raw-bundle or different-envelope revisions. This does not broaden the narrower service-only hosting profile, which permits internal model-gateway connections only.

## Remote helper enrollment administration

The operator API exposes read-only `GET /api/helpers/readiness`. It reports
current binding state, bounded enrollment/session projections, and whether
current signed qualification evidence contains a host-class record matching the
active binding's role, image and weights digests, destination, trust tier,
retention, and cancellation bound. Malformed, stale, oversized, or misbound
records produce fixed error codes. This endpoint is diagnostic only:
`dispatch_authority` is always false, and qualification records do not prove
live host state or enable dispatch.

The operator API exposes `POST /api/helpers/enrollments/{enrollment_id}` to provision an enrollment, `POST /api/helpers/enrollments/{enrollment_id}/rotate`,
`POST /api/helpers/enrollments/{enrollment_id}/revoke`, and
`POST /api/helpers/reconcile`. Each route requires the existing operator identity;
caller and gateway credentials are not accepted. Provisioning accepts bounded `binding_id`, `installation_id`, `role`, and `certificate_pem` fields and returns only `{enrollment_id,provisioned:true}`. The selected active binding must declare that enrollment role. It records a client certificate that has already been issued by the separately managed helper CA. Rotation accepts a bounded
`certificate_pem` body containing a client certificate that has already been
issued by the separately managed helper CA. The CA and private signing key stay
outside Sovite. Sovite validates client-certificate usage and enrollment key
type, provisions the enrollment or atomically rotates its certificate, and
fences sessions using the prior certificate. Revocation marks the enrollment revoked and fences its active
sessions in the same transaction. Reconciliation returns per-binding helper
demand and fences sessions whose enrollment expired or was revoked, or whose
binding generation/role is no longer current. Mutations and demand reconciliation
append actor-attributed durable audit events in their state transaction.

These routes do not implement CSR issuance or a helper renewal handshake. They
do not return certificate material or echo certificate parsing errors.

## Remote worker execution API

The versioned external worker API exposes `POST /internal/execution/v1/{pull,
start,renew,status,handback,complete,lookup}`. It mounts only when dedicated
`SOVITE_EXECUTION_TOKENS` are configured and the Python 3.12+ generated-contract
runtime is installed. Tokens map configured workload identities to unique
values of at least 24 characters. `compute_id` comes from `SOVITE_COMPUTE_ID`;
neither identity is accepted in request JSON. Execution tokens must be distinct
from operator, caller, author, activator, and inference-authority credentials.

External pull and start use only the admitted `agent-http-v1` profile. Pull
returns a task and a minimal recipe projection with agent id, endpoint,
optional credential environment-variable name, idempotency flag, and timeout.
It does not return the resolved model recipe or any credential value. Tool-agent
and gateway jobs remain available to existing local worker routes and are not
reserved by this external profile. Repeated start never returns a start
capability. Generated request and successful response schemas are loaded from
the installed package's authenticated resources and validated at both owner and
client boundaries.

`renew` and `handback` use the exact execution tuple plus a unique
`operation_id`. `complete` uses that tuple plus `operation_id`, an outcome, an
optional finite error code, and an optional `result_json` string containing one
JSON object. These mutations journal their exact request digest in the existing
owner operation table; same-key retries return the committed result and changed
requests conflict. `status` and `lookup` are read-only, workload-scoped
observations. All requests have a 65,536-byte ceiling, with generated per
operation response bounds and no truncation.

Status reports only the exact fence's state, finite Job status, phase, lease
expiry, and deadline. Lookup returns journal metadata without private request
or response bodies. The API retains the existing owner transaction, audit,
replay, cancellation, recovery, and authorization guards; no second queue or
lease authority is introduced.

## Execution bounds

The agent max_steps bounds Sovite's tool-agent loop, not native CLI turns. The service and worker use execution timeouts, lease renewal and cancellation. The worker applies the lower of the grant timeout and SOVITE_JOB_TIMEOUT_SECONDS. New native launchers bound stdout to 4 MiB, suppress provider stderr and terminate their owned process group on cancellation or oversized output, including children surviving the leader.

Native internal provider calls do not each run the worker's outbound authorization callback; the service boundary, periodic authority renewal and final release check govern them. Technical retry and idempotent replay are separate from a provider controller's semantic repair/review loop. Runtime success and deployment qualification are not acceptance verdicts.

### Upstream interface references

The native routes follow [Codex CI/CD authentication](https://developers.openai.com/codex/auth/ci-cd-auth), [Codex configuration](https://developers.openai.com/codex/config-reference), [Claude Code authentication](https://code.claude.com/docs/en/authentication), [Claude CLI options](https://code.claude.com/docs/en/cli-reference), [OpenCode Go](https://opencode.ai/v2/docs/console/go/), and the [OpenCode v2 commands](https://opencode.ai/v2/docs/cli/commands/) and [configuration](https://opencode.ai/v2/docs/config/). Tests use synthetic credentials and responses, plus local subprocesses for cancellation/output bounds. They do not establish live subscription/account qualification.
