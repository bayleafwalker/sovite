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

The internal worker API exposes `POST /internal/execution/pull`, `start`,
`renew`, `status`, `handback`, `complete`, and `lookup`. It authenticates only
dedicated `SOVITE_EXECUTION_TOKENS`, a JSON object mapping workload identities
to unique tokens of at least 24 characters. `compute_id` comes from the
service's `SOVITE_COMPUTE_ID`; neither identity is accepted in request JSON.
Execution tokens must be distinct from operator, caller, author, activator, and
inference-authority credentials. An empty execution-token map denies every
remote execution request.

`pull` accepts exactly `{instance_id,operation_id}`. It returns `idle` or the
existing bounded reserved job projection. `start` accepts exactly
`{instance_id,operation_id,job_id,generation,fence}`. The first committed start
may return the transient gateway execution capability; an exact replay returns
`already_started` without that capability. The operation key is
`(workload_id,instance_id,operation_id)`, bound to operation kind and canonical
request digest.

`renew` and `handback` use the same exact execution tuple plus a unique
`operation_id`. Their responses are journaled with the state change. Repeating
the same operation returns its original finite response; changing any request
field or operation kind under the same key returns `idempotency_conflict`.
`renew` can extend only a live started fence and never moves the fixed execution
deadline. `handback` records the existing cancellation, safe retry, or
`execution_uncertain` disposition and cannot release a replacement fence.

`status` accepts the exact tuple without an operation id. It returns one of
`current`, `superseded`, `finished`, or `expired`, with only the finite job
status, phase, lease expiry, and fixed deadline. It does not reveal task text,
resolved configuration, capabilities, credentials, or audit payloads.
`lookup` accepts `{instance_id,operation_id}` and reads only that workload's
existing operation journal; its projection includes operation kind, job/fence
identity, and result state, never a stored response body or start capability.
Both reads are side-effect free.

`complete` accepts the exact tuple plus an operation id, an outcome from
`completed`, `failed`, `retryable_failure`, or `cancelled`, an optional finite
error code, and for `completed` a JSON result object no larger than 48 KiB.
The whole HTTP body remains bounded by the configured request limit. Sovite
stores the result through its existing artifact store, then rechecks the exact
fence and current authority before publishing the artifact reference and
terminal job state in the owner transaction. A failed or uncertain result
cannot release content. `retryable_failure` is retried only when the installed
owner policy permits and the job is replay-safe; otherwise it is failed or
blocked as uncertain. Error codes are finite; raw exceptions, task text,
credential data, SQL state, and artifact bytes are not written to the operation
journal or audit event.

All mutation paths acquire the existing authority lock and exact Job/lease row
locks, append audit with the transition, and journal the committed result. A
response loss is reconciled by retrying the same operation identity or by the
read-only `lookup`. No operation replays a start capability, chooses a newer
fence, or creates a second job/lease authority. The HTTP surface is a local
candidate implementation; no deployment or runtime qualification is implied.

An agent-http harness served here declares exactly one native capability: native-codex, native-claude or native-opencode. Agents have an empty declared HTTP tool list; the service does not translate HTTP bindings into native authority. Multiple fixed agents require separate services. Enforced native executables cannot run inline in the worker. The service receives no platform database, artifact-store or qualification signing credentials. `SOVITE_HARNESS_TOKEN` is separate from subscription credentials and operator/caller tokens.

The ledger stores request digest and execution state. An exact completed replay returns its stored result; changed input under the same task ID is rejected. In-progress or uncertain work is not restarted. `POST /cancel` accepts `{task_id,agent_id,configuration_revision}` and requests cancellation of queued or running work. Remote cancellation is best effort; worker release authority remains independently checked.

Native output has `{kind,answer,agent_id,model_id,generation,isolation}`, with kind native-codex-result, native-claude-result or native-opencode-result. New subscription/Go output includes model identity with `immutable_weights_verified:false`. The worker stores the artifact and derives release from trusted state and the grant contract. Readiness checks only the fixed configuration and binary; live authentication, entitlement and provider compatibility require a qualified task.

### Native authentication and provider selection

| Route | Model declaration | Harness credential declaration |
| --- | --- | --- |
| Codex subscription | adapter codex-subscription; exact model; identity_protocol native-subscription; public cloud destination | `authentication: {mode: subscription, credential_file_env: SOVITE_CODEX_AUTH_FILE}` |
| Claude subscription | adapter claude-subscription; exact model; identity_protocol native-subscription; public cloud destination | `authentication: {mode: subscription, credential_env: SOVITE_CLAUDE_OAUTH_TOKEN}` |
| OpenCode Go | adapter opencode-go; model deepseek-v4.1-flash; identity_protocol provider-service; endpoint https://opencode.ai/inference/go/openai/v1; wire_api chat; api_key_env SOVITE_OPENCODE_GO_KEY | No subscription-authentication object; the model's explicit environment reference supplies the Go subscription key. |

Subscription declarations reject API endpoint/key fields and fake weight digests. Codex uses its native ChatGPT provider and exact model; Claude uses its explicit subscription OAuth token. Enforced destination hosts include chatgpt.com and auth.openai.com for Codex, api.anthropic.com for Claude, or opencode.ai for Go. Unavailable routes fail; Sovite selects no replacement model or account.

Each new invocation gets a fresh home, XDG directories, task directory and allowlisted environment. Codex copies only the explicitly selected, protected auth.json, holds a local sidecar lock, and atomically persists valid refreshed credentials after success, failure or cancellation. It refuses to overwrite an already changed source. Operators serialize the profile across hosts and coordinate rotation using the lock. Claude receives only the referenced token. Ambient API keys, provider URLs and home settings are not forwarded.

The new launchers provide answer-only inference. Codex disables shell/image tools, apps, subagents, hooks and remote plugin discovery with a read-only sandbox. Claude uses bare noninteractive mode, an empty tool set and strict empty MCP configuration. OpenCode requires v2, runs a standalone server, permits only opencode-go, denies tools/integrations and uses the exact model for maintenance inference too. Temporary state is removed after execution. These controls do not replace installed machine policy, deployment isolation, destination enforcement or qualification.

Go's direct tool-agent path supports Sovite's declared tools. It sends a job-stable x-opencode-session, an identifying User-Agent and the exact model, and rejects a response naming another model.

## Execution bounds

The agent max_steps bounds Sovite's tool-agent loop, not native CLI turns. The service and worker use execution timeouts, lease renewal and cancellation. The worker applies the lower of the grant timeout and SOVITE_JOB_TIMEOUT_SECONDS. New native launchers bound stdout to 4 MiB, suppress provider stderr and terminate their owned process group on cancellation or oversized output, including children surviving the leader.

Native internal provider calls do not each run the worker's outbound authorization callback; the service boundary, periodic authority renewal and final release check govern them. Technical retry and idempotent replay are separate from a provider controller's semantic repair/review loop. Runtime success and deployment qualification are not acceptance verdicts.

### Upstream interface references

The native routes follow [Codex CI/CD authentication](https://developers.openai.com/codex/auth/ci-cd-auth), [Codex configuration](https://developers.openai.com/codex/config-reference), [Claude Code authentication](https://code.claude.com/docs/en/authentication), [Claude CLI options](https://code.claude.com/docs/en/cli-reference), [OpenCode Go](https://opencode.ai/v2/docs/console/go/), and the [OpenCode v2 commands](https://opencode.ai/v2/docs/cli/commands/) and [configuration](https://opencode.ai/v2/docs/config/). Tests use synthetic credentials and responses, plus local subprocesses for cancellation/output bounds. They do not establish live subscription/account qualification.
