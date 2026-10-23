# Console API contract

All `/api/*` endpoints except `POST /api/auth/session` and the two hosted bundle endpoints below require operator authorization via `Authorization: Bearer …` or the HttpOnly `sovite_session` cookie. Cookie mutations require an exact `Origin` matching `SOVITE_PUBLIC_ORIGIN`. Requests and responses are JSON. The static console and API use the same origin. No operator credential is stored in browser localStorage. Errors use `{detail:{code,message}}`; messages never contain model or tool output.

Hosted installations only (a `SOVITE_HOSTING_ENVELOPE` is configured on the service) expose the author/activator bundle routes. They are not operator routes, and the operator token is refused on both. `sovite compile` and `sovite activate` are clients of these routes: they use `SOVITE_CONTROL_URL` plus `SOVITE_AUTHOR_TOKEN` or `SOVITE_ACTIVATOR_TOKEN`, need no database credential, and are the only hosted CLI paths for compilation and activation.

- `POST /api/bundles` body `{bundle:object}` requires the author role and returns 201 `{status:"compiled",generation:string}`. It stores a draft revision and does not change the active generation. A bundle that exceeds the current policy or hosting envelope returns 422.
- `POST /api/bundles/{generation}/activate` takes no body, requires the activator role and returns 200 `{status:"activated",generation:string}` for the requested generation. Misuse returns 401/403/404/409/422. There is no hosted `reload`.

- `POST /api/auth/session` body `{token:string}` creates a cookie and returns `{identity:"operator",role:"operator"}`. `GET /api/auth/session` returns the authenticated identity. `DELETE /api/auth/session` signs out.
- `GET /api/overview` returns `{name,mode,generation,counts:{agents,models,harnesses,grants,jobs,running,queued,completed,blocked},workers:[{id,updated_at,job_id,healthy}],qualification_required,qualified,audit:{valid,events,head,limitation},storage:{backend},security:{public_origin,secure_cookies},capabilities:{...}}`.
- `GET /api/catalog` returns arrays `models,harnesses,agents,tools,compute,destinations,data_classes,profiles`, plus `generation`. These preserve the declarative bundle fields. Empty arrays are valid.
- `GET /api/grants` returns `{items:[{...declaredGrant,inhibited,reason,updated_at,qualified}]}`.
- `POST /api/grants/{id}/inhibit` body `{reason:string}` inhibits dispatch and release; active jobs receive cancellation intent. `POST /api/grants/{id}/resume` body `{reason:string}` clears a runtime inhibit only when current config still enables the grant and current generation is qualified.
- `GET /api/jobs?limit=100` returns `{items:[Job]}`. `POST /api/jobs` body `{grant_id:string,task:string,idempotency_key?:string,input_class?:string}` returns a private `Job` object. The operator is not forced into a grant's external caller list; authorization uses a deliberate operator path and still enforces class, qualification and inhibits.
- `GET /api/jobs/{id}` returns a `Job` with `task` and `resolved` added. `POST /api/jobs/{id}/cancel` requests cancellation. `GET /api/jobs/{id}/artifact` returns private JSON, or 409 if no completed artifact exists. Artifact payload can contain sensitive data; show only after deliberate operator interaction.
- `Job` = `{id,receipt,grant_id,caller,generation,status,created_at,updated_at,started_at,finished_at,attempts,next_attempt_at,cancel_requested,error_code,artifact_available,released_at}`. Times are Unix seconds. States: `queued,running,retry_wait,completed,failed,cancelled,blocked`. A completed private result may be retained without a public release if authority was revoked. `error_code` is a fixed safe enum/string, never exception text. Do not infer qualification or readiness from a catalog item.
- `GET /api/receipts` returns `{items:[{receipt,grant_id,caller,status,created_at,released_at,job_id}]}` for operators only. Public MCP projections never include `job_id`, task, result URL, error text or tool outputs.
- `GET /api/audit?limit=200` returns `{items:[{sequence,created_at,actor,action,target,detail,previous_hash,hash}],verification:{...}}`. `detail` contains bounded operational metadata, never task/model/tool payloads.
- `GET /api/qualifications` returns `{items:[{id,generation,profile,created_at,expires_at,passed,issuer,valid,payload,signature,public_key}],required}`. Qualification creation occurs through CLI, not UI.
- `GET /api/deployments` returns `{items:[{id,generation,current,created_at,mode,name,components:{...}}]}` for loaded desired bundle revisions. These are configuration revisions, not claims that Kubernetes reconciliation completed.
- `GET /health/live` returns `{"status":"ok"}`. `GET /health/ready` checks DB and private artifact backend; it does not assert model quality or upstream readiness.

`POST /mcp` is an authenticated JSON-RPC endpoint for declared caller tokens (operator token is rejected). Allowed methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. Allowed tools: `begin_task({grant_id,task,idempotency_key,input_class?})` and `task_status({receipt})`. Public responses contain opaque receipt plus fixed status only. Private task APIs, resource methods, sampling, arbitrary notifications, and result fetching are unsupported and rejected. This transport implements bounded request/response JSON without server-side SSE streams.

## External native harness contract

The worker sends a private request to the configured `agent-http` harness endpoint. The isolated native service implements `POST /run` with the following exact body:

```json
{
  "task_id": "durable-job-id",
  "agent_id": "code-review",
  "configuration_revision": "sha256:declared-bundle-generation",
  "input": {"task": "Read the configured workspace and complete this task."}
}
```

The request uses the configured service bearer token and `Idempotency-Key: durable-job-id`. The service binds its agent, model, executable, workspace and bundle generation at startup. A request cannot replace those bindings or add endpoints and tool configuration. `SOVITE_HARNESS_TOKEN` authenticates this private service boundary; it is distinct from operator and MCP caller tokens. The supplied deployment does not give the native service platform database, object-store or qualification signing credentials.

The service persists a request digest and execution state in its own SQLite ledger. An exact completed replay returns the stored private result. A changed request under the same task identity is rejected. An in-progress or uncertain execution is not started again. `POST /cancel` accepts `{task_id,agent_id,configuration_revision}` with the service bearer token and requests cancellation of running or queued work. Remote cancellation is best effort; the worker independently rechecks authority before releasing a result.

Successful native output is `{kind:"native-codex-result",answer,agent_id,model_id,generation,isolation}`. The worker wraps that result, commits it to private artifact storage and derives the public receipt from trusted state. The service's `/health/ready` checks the fixed configuration and native executable only. Provider and model compatibility require an actual task.

## Execution bounds

`agents[].max_steps` limits model/tool iterations in Sovite's `tool-agent` harness. It does not impose a turn count on the maintained native Codex CLI or an independently deployed external agent. The native service and worker use their configured execution timeouts, lease renewal and cancellation. The worker applies the lower of the grant timeout and `SOVITE_JOB_TIMEOUT_SECONDS`. Native internal provider calls do not each invoke the worker's outbound authorization callback; the separate service boundary, periodic authorization renewal and final release check govern that path.
