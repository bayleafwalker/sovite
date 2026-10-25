# Worker remote boundary status

Status: **The source has a scoped execution-owner HTTP API, an opt-in HTTP
client, an opt-in remote worker composition, and a standalone launch command.
The default worker still uses SQL and S3 directly.** This note records source
state, not deployment or qualification.

## Current source path

`api.py` registers `/internal/execution/*`. `sovite.execution_client.ExecutionClient`
provides explicit calls for pull/start/renew/status/handback/complete/lookup. It
is not selected by default worker composition; callers must construct it with
an HTTPS origin and dedicated execution token or explicitly load those from
`SOVITE_CONTROL_URL` and `SOVITE_EXECUTION_TOKEN`. Server authentication uses
the matching workload token configured through `SOVITE_EXECUTION_TOKENS`.

The standalone CLI now requires an absolute `SOVITE_EXECUTION_JOURNAL_PATH`.
It holds a nonblocking exclusive process lock on a companion lock file and
uses an owner-only, atomically replaced and fsynced journal. It rejects
symlinks, non-regular files, foreign ownership, group/world permissions,
oversized or structurally complex JSON, duplicate keys, and non-finite
numbers. The single-record journal contains only instance/operation/job IDs,
operation kind, phase, expected result states, generation/fence and bounded
lease/deadline timestamps. It never stores URLs, tokens, gateway capabilities,
task/resolved input, runner output, or exception text.

Before each pull/start/renew/handback/complete mutation the worker persists the
exact operation key and expected outcome. Confirmed idle pulls and confirmed
handback/complete outcomes clear the record; mutation results update the active
fence record. On startup, pending keys are reconciled by exact owner lookup and
status is used only to inspect a known fence. A recovered active or reserved
fence is never resumed and never triggers another pull; the worker preserves a
manual-reconciliation record and stops. A pull lookup that finds a reservation
cannot recover task/context, so that reservation is left for owner expiry.
The local journal is only a crash-recovery cache, not authority, and cannot be
transferred across hosts as proof of ownership.

`sovite-remote-worker` starts `RemoteWorker` using only the execution API
client. It requires `SOVITE_EXECUTION_INSTANCE_ID`, `SOVITE_CONTROL_URL`, and
`SOVITE_EXECUTION_TOKEN`, with optional bounded poll and lease interval
settings and a mounted `SOVITE_EXECUTION_CA_BUNDLE` for private trust roots.
Its startup path does not construct `Settings`, `Database`, or
`Platform`; it refuses to start when local database, artifact-store, or
administrative credential settings are present. Native subprocesses receive
the existing allow-listed environment, which excludes the execution bearer.
This command is an explicit source candidate; existing `sovite worker` and
embedded-worker composition remain unchanged.

Gateway-backed execution in the opt-in remote worker is limited to `tool-agent`
jobs with an `openai-compatible` Chat binding. Configure the fixed HTTPS
inference origin with `SOVITE_INFERENCE_URL`, a distinct workload bearer with
`SOVITE_INFERENCE_TOKEN`, and, when required, a readable
`SOVITE_INFERENCE_CA_BUNDLE`. The runner binds the one-time gateway capability
to the exact binding, generation, job, workload, fence and deadline returned by
the started owner operation. It sends only the selected Chat request through
`/inference/{binding_id}/chat`; model declarations cannot choose the URL or
carry an upstream credential. The per-request deadline is bounded by both the
started job deadline and the binding timeout. Unsupported harnesses, wire APIs,
missing credentials, malformed responses and gateway errors fail closed. The
default SQL/S3 worker and native harnesses are not routed through this client.

The client makes one bounded HTTP request per invocation, disables ambient
proxy and redirect behavior, and never retries a mutation. Transport loss,
malformed success data, or a server failure during a mutation raises
`ExecutionOutcomeUnknown` with the operation ID and directs the caller to
lookup using the same instance and operation IDs; it exposes both fields as
`instance_id` and `operation_id`. A lookup 404 raises
`ExecutionLookupNotFound` with that same key and explicitly leaves the original
outcome unknown. A missing journal row is not permission to replay the
mutation. The error does not include
response content, the bearer token, URL credentials, or exception text. The
client preserves the captured job, generation, and fence and does not
substitute a newer context. TLS verification cannot be disabled; a custom CA
path is supported. Response bytes are bounded and structurally preflighted
before parsing, duplicate JSON keys and non-finite constants are rejected, and
all seven operation projections receive finite shape checks. Pull's task and
resolved objects are checked as dictionaries but their private internal
schemas remain provisional; returned values are visible to the explicit
caller. Pull necessarily returns task and resolved execution input, which
callers must keep out of logs. Start's `already_started` response is checked
to contain no execution bearer.

The API and client use the local P08 request shapes and the owner journal. Pull
and start have no bearer-replay path: the first start response may contain
short-lived execution material, while a replay returns owner metadata without
that material. Renew, handback, and complete are journaled; status and lookup
are observations. Lookup is scoped to the exact instance and operation ID and
returns fixed journal metadata. Callers should generate a distinct operation
ID for each new mutation and use the original ID for exact reconciliation.

## Remaining boundary

The local `Worker.claim`, renewal, shutdown handback, recovery, and finalization
still call `LeaseService` against `Platform.db.sessions`. `Worker.execute`
calls `Platform.store.put` directly before fenced SQL finalization. The
opt-in `RemoteWorker` instead sends a bounded result through the owner
`complete` operation, whose existing Platform path writes the artifact and
fences release. The default local worker and its Compose/Helm requirements are
unchanged. Native harness composition remains separate.

The current source does not include a generated external worker contract or
qualification evidence for real HTTP owner races, revocation, fences, or
response-loss histories. `tests/test_worker_remote_boundary.py` still asserts
route absence and is stale against the registered execution routes; it was not
run or edited in this slice. Existing remote-operation contract notes also
need reconciliation against the route and current owner implementations.

The journal cannot recover a lost task/context payload or the one-time gateway
capability. A restarted process therefore cannot resume a gateway request; it
requires exact owner reconciliation and will not repeat the model request. The
local routing candidate does not qualify cancellation bounds, gateway reach,
credential delivery, identity checks or cross-host restart behavior.

The remote command routes only the exact gateway-backed tool-agent path above;
it does not route native harnesses or change the existing worker deployment.
Automatic restart cannot continue a predecessor process. Do not infer
credential cutover, worker-host acceptance, artifact integrity, deployment, or
live qualification from these source APIs. Real HTTP owner races, revocation,
fences, response-loss histories, gateway cancellation bounds, artifact
integrity, and PostgreSQL locking still require disposable qualification.
