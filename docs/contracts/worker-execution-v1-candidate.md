# Worker execution wire contract candidate

Status: **generated F2 candidate with executable SQLite/PostgreSQL evidence; not deployed or fully O2/F2-qualified.**

This note records Sovite's versioned `agent-http-v1` execution projection. The
routes use the existing owner rows and operation journal. The accepted external
projection is deliberately narrower than the local worker's resolved recipe;
this source candidate does not authorize deployment or live credential cutover.

The companion [requirements document](worker-execution-remote-operations-requirements.md)
records the selected P08 semantics for renew, status, handback, complete, and
lookup, including bounded requests, replay behavior, and exact fencing.

The implementation uses the condition in
[`2026-10-23-execution-lease-owner.md`](../plans/2026-10-23-execution-lease-owner.md).
The contract is generated against Vuoro foundation commit
`b105f64ce5c12399e04c5d19e72d6cd69bc029f`. Runtime validation uses the
published `vuoro-contracts` 0.2.0 wheel pinned by SHA-256
`7bc6558076fff11591b6382641608a95afebb846c254309673351a29854e12c9`. The
installed package carries its OpenAPI, JSON Schema, operation manifest and
nested integrity inventory. The owner API and client validate successful wire
requests and responses from that bundle. Deployment and full runtime
qualification remain open.

## Authenticated scope

The bearer maps to a configured workload; `compute_id` comes from owner
configuration. Neither is accepted from request JSON. A request cannot set a
caller, worker role, qualification, grant, model, owner lease key, expiry, or
fence. The inference gateway capability is a different credential and never
authenticates the worker API. Routes are mounted only when execution tokens are
configured and the Python 3.12+ contract runtime is installed.

The owner currently accepts these exact bounded identifier forms:

| Field | Candidate wire rule | Owner evidence |
| --- | --- | --- |
| `instance_id` | 1–64 ASCII letters, digits, `_`, or `-` | `LeaseService` validation |
| `operation_id` | 1–64 ASCII letters, digits, `_`, or `-` | journal primary key scope |
| `job_id` | nonempty string, at most 64 characters | `start` validation |
| `generation` | `sha256:` followed by 64 lowercase hex digits | lease/job generation match |
| `fence` | exactly 32 lowercase hex digits | current lease incarnation |

This spelling is copied from the internal owner validators. The generated wire
schema preserves it without coercion and documents field and operation byte
limits.

## Owner-backed request and response shapes

The generated source is `contracts/execution-v1/main.tsp`. `instance_id` remains
in each request body, and the HTTP paths are
`/internal/execution/v1/{pull,start,renew,status,handback,complete,lookup}`.
`LeaseService` retains its private local-worker projection; external pulls use
a precommit `agent-http-v1` admission and a fixed `recipe` projection.

### Pull / reserve

Request JSON has exactly `instance_id` and `operation_id`:

```json
{"instance_id":"worker-instance-1","operation_id":"pull-1"}
```

The owner binds the operation digest to the authenticated principal, operation
kind and exact request. The durable operation key is
`(workload_id, instance_id, operation_id)`. Same-key/different-request or
different-kind reuse is `idempotency_conflict` (409).

The owner returns one of:

```json
{"state":"idle"}
```

or a reserved snapshot containing the task and only the admitted HTTP recipe:

```json
{"state":"reserved","job":{"job_id":"…","generation":"…","fence":"…","lease_until":0,"task":"…","recipe":{"profile":"agent-http-v1","agent_id":"…","endpoint":"…","idempotent":true,"timeout_seconds":300}}}
```

The recipe carries no resolved model, private recipe, or credential value. It
may name the worker's credential environment variable. Tool-agent and gateway
jobs remain available to their existing local routes and are left queued by
this first-profile pull route.

The implementation pins the complete canonical UTF-8 response at no more than
4 MiB before committing the reservation journal and audit. Reservation does
not charge an attempt, create a capability, or set the execution deadline.
An idempotent live replay rechecks current authority and reconstructs private
fields from the locked owner rows; it does not trust a cached response body.

### Start

Request JSON has exactly `instance_id`, `operation_id`, `job_id`, `generation`,
and `fence`. The fence and generation must match the current live reservation.
The first committed start advances the attempt exactly once and fixes the
execution deadline.

The external agent-http start response has `state=started`, `job_id`,
`generation`, `fence`, `lease_until`, and `deadline`. It never returns a gateway
capability. A repeated start returns only
`{state=already_started,job_id,generation,fence}`; it never returns the bearer
again, even when the caller supplies a different operation ID. A caller that
loses the first response must stop. Status/reconciliation cannot mint or
re-deliver that capability.

### Renew, status, handback, complete, and lookup

The versioned routes expose these operations at `/internal/execution/v1/{renew,
status,handback,complete,lookup}`. `renew`, `handback`, and `complete` require a new operation
id and journal their exact request digest in the existing owner operation
table. Same-key retries return the recorded response; changed requests conflict.
Status and lookup are read-only, workload-scoped observations. Their precise
body and response shapes, including completion bounds and finite errors, are
recorded in the [remote operation requirements](worker-execution-remote-operations-requirements.md)
and [API contract](../API_CONTRACT.md).

A lookup of an exact idle pull returns `job_id: null` and `fence: null`, with
`operation_kind: pull` and `result_state: idle`. There was no reservation to
bind. A restarted worker can reconcile that recorded result without repeating
the pull. Null bindings for other results, extra fields in an idle owner result,
or a job/fence attached to an idle result remain owner-corruption errors.
Lookup does not change the journal, audit, queue or lease rows. The caller's
existing workload and instance scope still applies.

Status reports only the exact fence's state, finite Job status, phase, lease
expiry and deadline. If the supplied generation or fence has been replaced,
the response retains that caller-supplied tuple and sets phase, job status,
lease expiry, and deadline to `null`; it never labels replacement state as
belonging to the stale tuple. `current` requires live authorization as well as a live
lease: cancellation, inhibition, superseded binding, or withdrawn gateway
execution authority is reported as `superseded` without exposing a reason.
Status takes the existing authority lock and exact Job/lease row locks so its
authorization check and projection describe one owner observation. Handback
returns `handed_back` with the owner disposition.
Complete accepts a bounded `result_json` string containing a JSON object or a
finite failure/cancellation outcome; the API rejects duplicate keys and
non-finite numbers before Sovite writes result bytes through its own artifact
store and links them only after final fence and authorization checks. Lookup returns journal
metadata without private request/response bodies. None of these operations
returns a start capability.

The candidate must preserve owner-generated fences, current authorization,
response-loss safety, append-only audit and exact transaction boundaries. It
must not add another queue or lease authority.

## Isolated runtime composition

Compose and Helm expose an opt-in API-only worker. It is disabled by default,
requires an explicitly configured HTTPS control origin, instance identity and
execution token, and stores the crash-reconciliation journal on persistent
storage. Its environment and mounts contain no platform database URL/schema,
S3 endpoint/bucket or AWS storage key, operator/caller credential, owner token
map, bundle, or local control volume. Optional adapter credentials are named
individually and come only from the deployment secret. They must match the
`credential_env` admitted in an `agent-http-v1` recipe. The service never
inherits the local worker's shared runtime environment.

Helm's remote-worker egress remains default-deny. Operators must provide exact
owner and selected agent endpoint peers under `networkPolicy.remoteWorkerPeers`;
the HTTPS origin is not inferred from the chart's public-origin value. Compose
uses the separately supplied `SOVITE_REMOTE_CONTROL_URL`; it does not guess an
owner URL or route through the local HTTP control listener. The same instance
identity must remain attached to its persistent journal across process
restarts. Losing that journal is not permission to replay an unknown mutation.

This composition is source-level and opt-in. It does not authorize rollout or
prove a site's secret, network, or credential bindings. The local SQL/S3 worker
and its existing composition are unchanged.

## Error boundary

The route uses the existing bounded `PlatformError` body. Worker operations use
finite codes including `invalid_request` (422), `execution_scope_denied` (403),
`execution_fenced` (409), `idempotency_conflict` (409), `cancelled` (409),
`owner_unavailable` (503), `execution_payload_too_large` (413), and
`not_found` (404). No adapter serializes raw exception text, task content,
credentials, SQL state, or arbitrary stored data.

## Admission sequence

1. The consumer pins Vuoro contract runtime commit
   `b105f64ce5c12399e04c5d19e72d6cd69bc029f`; package resources are loaded with
   `importlib.resources` and checked against the nested integrity digest.
2. Installed-wheel checks verify the OpenAPI, schema, manifest, and integrity
   assets. API and client success paths validate request and response payloads
   with the generated bundle.
3. SQLite and disposable PostgreSQL runs cover response-loss replay, altered
   keys, unauthorized workload access, profile filtering, and the owner lease
   regression suite. The full lock-order and recovery history matrix remains
   part of qualification.
4. Review dedicated execution-token configuration and artifact response bounds
   before runtime adoption. Deployment remains outside this candidate.

The available run covered 287 tests across the remote boundary, lease suite,
and installed wheel. It does not cover the full listed race/recovery matrix,
end-to-end worker-host qualification, or authorize deployment.
