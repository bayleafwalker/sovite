# Worker execution wire contract candidate

Status: **local P08 API implementation candidate; not deployed or runtime-qualified.**

This note advances P05's F2 contract work from the owner implementation. It
records the local request/response shapes and their limits. The P08 candidate
adds authenticated internal worker routes over the existing owner rows; it does
not authorize deployment, credential removal, or runtime adoption.

The companion [requirements document](worker-execution-remote-operations-requirements.md)
records the selected P08 semantics for renew, status, handback, complete, and
lookup, including bounded requests, replay behavior, and exact fencing.

The implementation uses the condition in
[`2026-10-23-execution-lease-owner.md`](../plans/2026-10-23-execution-lease-owner.md):
the shared generated-contract foundation and real O2/F2 consumer qualification
remain required before deployment or runtime adoption. This local candidate is
available for those checks; passing source review alone does not qualify it.

## Authenticated scope

The caller identity must be established by the future authenticated adapter.
`workload_id` and `compute_id` are derived from that identity and are never
accepted from request JSON. A request cannot set a caller, worker role,
qualification, grant, model, owner lease key, expiry, or fence. The inference
gateway capability is a different credential and must not authenticate the
worker API.

The owner currently accepts these exact bounded identifier forms:

| Field | Candidate wire rule | Owner evidence |
| --- | --- | --- |
| `instance_id` | 1–64 ASCII letters, digits, `_`, or `-` | `LeaseService` validation |
| `operation_id` | 1–64 ASCII letters, digits, `_`, or `-` | journal primary key scope |
| `job_id` | nonempty string, at most 64 characters | `start` validation |
| `generation` | `sha256:` followed by 64 lowercase hex digits | lease/job generation match |
| `fence` | exactly 32 lowercase hex digits | current lease incarnation |

This spelling is copied from the internal owner validators. A generated wire
schema must preserve it without coercion and add a documented character/byte
limit for every field before publication.

## Owner-backed request and response shapes

These shapes are already implemented by `LeaseService.reserve` and
`LeaseService.start`; they are the only response shapes that this proposal can
currently pin. `task` and `resolved` are internal owner values, not yet an
approved external projection.

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

or an internal reserved snapshot:

```json
{"state":"reserved","job":{"job_id":"…","generation":"…","fence":"…","lease_until":0,"task":"…","resolved":{}}}
```

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

First start response has `state=started`, `job_id`, `generation`, `fence`,
`lease_until`, and `deadline`. Gateway jobs additionally have
`execution={capability,context}`. A repeated start returns only
`{state=already_started,job_id,generation,fence}`; it never returns the bearer
again, even when the caller supplies a different operation ID. A caller that
loses the first response must stop. Status/reconciliation cannot mint or
re-deliver that capability.

### Renew, status, handback, complete, and lookup

P08 exposes these operations at `/internal/execution/{renew,status,handback,
complete,lookup}`. `renew`, `handback`, and `complete` require a new operation
id and journal their exact request digest in the existing owner operation
table. Same-key retries return the recorded response; changed requests conflict.
Status and lookup are read-only, workload-scoped observations. Their precise
body and response shapes, including completion bounds and finite errors, are
recorded in the [remote operation requirements](worker-execution-remote-operations-requirements.md)
and [API contract](../API_CONTRACT.md).

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
Complete accepts one bounded JSON result or a finite failure/cancellation
outcome; Sovite writes result bytes through its own artifact store and links
them only after final fence and authorization checks. Lookup returns journal
metadata without private request/response bodies. None of these operations
returns a start capability.

The candidate must preserve owner-generated fences, current authorization,
response-loss safety, append-only audit and exact transaction boundaries. It
must not add another queue or lease authority.

## Error boundary

The route uses the existing bounded `PlatformError` body. Worker operations use
finite codes including `invalid_request` (422), `execution_scope_denied` (403),
`execution_fenced` (409), `idempotency_conflict` (409), `cancelled` (409),
`owner_unavailable` (503), `execution_payload_too_large` (413), and
`not_found` (404). No adapter serializes raw exception text, task content,
credentials, SQL state, or arbitrary stored data.

## Admission sequence

1. Pin the shared namespace/resource-root foundation and generated client
   commits in this consumer.
2. Build ordinary installed-artifact consumers from the generated contract;
   reject arbitrary IDs, coercions, oversized bodies, and response canaries.
3. Qualify O2/F2 through real HTTP and the owner against disposable SQLite and
   PostgreSQL, including response loss after commit, same/different operation
   keys, concurrent starts, current/revoked authorization, expired fences,
   handback/recovery races, and complete/lookup isolation.
4. Review the dedicated execution-token configuration and artifact response
   bounds before runtime adoption. Deployment remains outside this candidate.

This source candidate has not run the listed qualification histories and does
not establish end-to-end worker qualification or authorize deployment.
