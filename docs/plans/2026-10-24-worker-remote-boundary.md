# Worker remote boundary status

Status: **The source has a scoped execution-owner HTTP API and a standalone
opt-in HTTP client. The default worker still uses SQL and S3 directly.** No
artifact-write API or remote-worker qualification is included. This note
records source state, not deployment or qualification.

## Current source path

`api.py` registers `/internal/execution/*`. `sovite.execution_client.ExecutionClient`
provides explicit calls for pull/start/renew/status/handback/complete/lookup. It
is not selected by default worker composition; callers must construct it with
an HTTPS origin and dedicated execution token or explicitly load those from
`SOVITE_CONTROL_URL` and `SOVITE_EXECUTION_TOKEN`. Server authentication uses
the matching workload token configured through `SOVITE_EXECUTION_TOKENS`.

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

`Worker.claim`, renewal, shutdown handback, recovery, and finalization still
call `LeaseService` against `Platform.db.sessions`. `Worker.execute` calls
`Platform.store.put` directly before fenced SQL finalization. SQL and S3 do not
share a transaction, and this source slice adds no artifact upload/write API.
The default Compose and Helm worker therefore still require SQL and object
store access. Native harness composition remains separate.

The current source does not include a generated external worker contract or
qualification evidence for real HTTP owner races, revocation, fences, or
response-loss histories. `tests/test_worker_remote_boundary.py` still asserts
route absence and is stale against the registered execution routes; it was not
run or edited in this slice. Existing remote-operation contract notes also
need reconciliation against the route and current owner implementations.

Do not infer credential cutover, worker-host acceptance, artifact integrity,
deployment, or live qualification from the presence of these source APIs. The
next source work is generated contract/schema qualification, followed by a
separate artifact-write boundary and an explicit worker composition change.
