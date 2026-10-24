# Worker execution wire contract candidate

Status: **source proposal only; no external contract or route is admitted.**

This note advances P05's F2 contract work from the owner implementation on
`cloud/20261024/programme-08-integration-sovite-exact` at
`cd4d672ea50dca472c87bbba49034311ff70320d`. It records the owner-backed
request and response semantics that can be frozen without turning the internal
Python API into a network interface. It does not authorize a worker API,
generated package, credential removal, deployment, or runtime adoption.

The remaining remote operations are scoped in the companion
[requirements document](worker-execution-remote-operations-requirements.md).
It records owner decisions needed for renew, status, handback, complete, and
lookup; it does not freeze their wire semantics.

The admission condition remains the one in
[`2026-10-23-execution-lease-owner.md`](../plans/2026-10-23-execution-lease-owner.md):
the shared generated-contract foundation must be accepted, and the real O2/F2
consumer qualification must pass. Until then, this document is a review
artifact. The API-absence assertion in `tests/test_worker_remote_boundary.py`
remains required.

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

No remote request or response shape is frozen for these operations. Existing
`renew`, `hand_back`, `finish`, recovery, and row inspection are internal
owner/worker methods with transaction-local context; they do not yet provide
operation-journaled external contracts. The route/client must not infer a
success response from these Python return values.

Before defining these wire operations, the owner contract must decide and test:

1. Whether renew is journaled and how a lost renew response is reconciled
   without extending the lease twice or reviving a revoked capability.
2. Which current owner fields status may expose for the authenticated workload
   and exact `(instance_id, job_id, generation, fence)` scope.
3. Whether handback is an idempotent action or an observation after unknown
   outcome, including what it returns when recovery has already advanced the
   fence.
4. How complete records artifact references and sanitized runtime errors while
   preserving the current owner transaction and uncertain external effects.
5. How lookup distinguishes idle, not-found, expired, superseded, and unknown
   journal state without authorizing another pull or replay.

The candidate must preserve owner-generated fences, current authorization,
response-loss safety, append-only audit and exact transaction boundaries. It
must not add another queue or lease authority.

## Error boundary

The current owner has regression-covered outcomes including `invalid_request`
(422), `execution_scope_denied` (403), `idempotency_conflict` (409),
`execution_fenced` (409), `cancelled` (409), `binding_inhibited` (403), and
`owner_unavailable` (503). Authorization and lineage checks also preserve
current owner outcomes such as forbidden, unavailable, unfinished predecessor,
and uncertain predecessor effects. These are internal `PlatformError` outcomes;
they are not yet a frozen HTTP error envelope. No adapter may serialize raw
exception text, task content, credentials, SQL state, or arbitrary stored data.

The generated contract must pin a finite error-code/status map and bounded
machine-readable body. Until then, callers must not depend on undocumented
internal error strings or treat an unclassified response as idle.

## Admission sequence

1. Accept the shared namespace/resource-root foundation and pin its exact
   contract/generator commits in this consumer.
2. Freeze the external task projection, authenticated identity mapping, error
   envelope, and the remaining five operation semantics with the F2 owner.
3. Build ordinary installed-artifact consumers from the generated contract;
   reject arbitrary IDs, coercions, oversized bodies, and response canaries.
4. Qualify O2/F2 through real HTTP and the owner against disposable SQLite and
   PostgreSQL, including response loss after commit, same/different operation
   keys, concurrent starts, current/revoked authorization, expired fences,
   handback/recovery races, and complete/lookup isolation.
5. Only after those checks are accepted, propose the narrowly scoped routes and
   worker credentials. Artifact writes require a separate one-use fenced
   boundary; direct SQL/S3 access remains until then.

The current source passes internal owner tests against disposable databases,
but steps 1–4 are not established by this proposal. This note must not be read
as API approval or as a claim of end-to-end worker qualification.
