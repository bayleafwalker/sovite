# Worker execution remote operations: requirements

Status: **owner-operation requirements; the first generated `agent-http-v1` profile is implemented as a candidate but not deployed or fully qualified.**

This document records shared owner-operation safeguards for remote workers.
The first generated `agent-http-v1` contract is recorded in
[`worker-execution-v1-candidate.md`](worker-execution-v1-candidate.md). This
broader document also retains later-profile requirements; qualification and
deployment remain separate work.

The existing Platform jobs, queue, authorization, audit, and universal execution
lease remain the only state authority. The identity, fence, digest, and
transaction rules below inherit the internal owner decisions in
[`2026-10-23-execution-lease-owner.md`](../plans/2026-10-23-execution-lease-owner.md).

## Shared identity and fence requirements

1. The authenticated adapter derives `workload_id` and `compute_id` from the
   authenticated principal. Neither is accepted in request JSON. Caller, role,
   qualification, grant, model, owner key, deadline, lease owner, and fence
   authority are likewise not caller-selectable.
2. Every operation is scoped to the exact tuple
   `(workload_id, instance_id, job_id, generation, fence)`. `instance_id` is
   the bounded ASCII identifier already used by reserve/start. `job_id`,
   generation, and fence retain the exact owner forms described in the
   candidate contract. The adapter must never resolve a partial tuple to the
   latest job incarnation.
3. The owner compares the complete tuple against the current Job and universal
   lease while holding the locks required for that state transition or
   observation. A reclaimed job has a new owner-generated fence. A request
   carrying the old fence cannot renew, publish status as current, hand back,
   complete, or read artifacts for the new incarnation.
4. A running worker closes over the fence it received at reserve. It must not
   substitute a newer in-memory `execution_fences[job_id]` value after
   same-instance reclaim. An external adapter cannot call the local trusted
   Worker binding interface or choose its owner key.
5. Every mutating operation has an authenticated owner principal and a
   canonical operation identity. **P08 choice:** renew, handback, and complete
   use the existing `(workload_id, instance_id, operation_id)` journal and bind
   operation kind plus exact validated request to its digest. Status and lookup
   are read-only and unjournaled. Same-key changed body, scope, principal, or
   kind returns `idempotency_conflict` (409), never a second transition.

## Response loss and idempotency

The caller must assume that a timeout can occur after the owner commits and
before a response arrives. A transport timeout is therefore an unknown outcome,
not evidence that the operation failed.

| Operation | Required treatment of a lost response |
| --- | --- |
| Renew | Same-key retry returns the committed `renewed` result without a second extension. It cannot extend a replacement fence or revive revoked authorization. A new operation against an expired/stale fence returns `execution_fenced` (409). |
| Status | A retry is an observation only; it cannot renew, start, reclaim, or authorize another pull. It has no operation id and returns `current`, `superseded`, `finished`, or `expired` plus a bounded minimal projection. |
| Handback | Same-key retry returns the committed disposition. A new-key call against a finished/recovered fence returns `execution_fenced`; a handback cannot release a later reservation. |
| Complete | Same-key retry returns the committed state and result digest without storing or publishing a second result. The owner writes bounded result JSON through its existing artifact store, then rechecks the fence and current authority before linking it. A losing/stale completion may leave only an unreferenced content-addressed artifact. |
| Lookup | Lookup reads one journal row scoped to authenticated workload, exact instance, and operation ID. Missing records return `not_found` (404); it never follows a newer fence or reveals stored request/response bodies. Rows remain under the existing owner retention behavior. |

The response-loss protocol must preserve append-only audit: a committed journal
result is not overwritten to make a retry appear fresh. If an operation is
unavailable or its committed result cannot safely be reconstructed, the owner
returns a bounded failure; it must never return `idle` or imply success. Reserve
and start retain their existing no-replay-bearer rules: none of these operations
may mint or re-deliver a start capability.

## Operation requirements and state outcomes

The following are owner invariants that apply across profiles. The generated
`agent-http-v1` source freezes its first-profile HTTP paths and payload shapes;
other profiles and external projections remain subject to their own admission
and qualification.

### Renew

- Renewal is conditional on exact workload, instance, job, generation, and fence
  matching the current running lease. It cannot change generation, fence,
  attempt, or the fixed execution deadline.
- Scheduling ownership in Job and the universal lease changes atomically. A
  `true`/success outcome is allowed only if both copies are extended coherently;
  partial extension is an owner failure and rolls back.
- Gateway renewal also requires current authorization and a live matching
  authority deadline. Digest withdrawal, expiry, or replacement must not revive
  capability. Gateway authority and both scheduling copies remain unchanged
  when renewal is refused.
- Existing trusted renewal returns false when the exact context is stale or
  authority is withdrawn. **P08 choice:** successful renewal returns
  `renewed` with the lease expiry and fixed deadline; refusal is a typed owner
  error and does not journal a success.

### Status

- Status reads only the caller's current exact execution context. It must not
  expose another workload's existence, task, resolved recipe, capability,
  credential, SQL details, or arbitrary audit payload.
- Status must identify, without ambiguity, whether the exact fence is current,
  finished, expired, superseded, cancelled, or unavailable. It must not imply
  that a `started` result includes a recoverable capability.
- **P08 choice:** the response contains only the exact tuple, lease phase,
  finite job status, lease expiry, and deadline. Finished and superseded fences
  are bounded observations; status does not reveal terminal reason or payload.

### Handback

- Handback operates only on the exact current fence and uses the existing owner
  transition that clears scheduling ownership and marks the old phase finished.
  It cannot mutate a later reservation.
- The outcome must distinguish successful release from a stale/recovered fence;
  a late handback cannot erase recovery evidence or change the next fence.
- **P08 choice:** handback is a journaled mutation. Its response is
  `handed_back` with disposition `retry_wait`, `blocked`, or `cancelled`.
  Existing replay-safety policy decides retry versus `execution_uncertain`.

### Complete

- Completion validates the exact fence, current authorization and cancellation
  state, then records the terminal Job/lease state, sanitized runtime outcome,
  artifact reference, operation result, and append-only audit in the owner
  transaction. No second queue or lease authority is introduced.
- **P08 choice:** completed output is a JSON object sent to Sovite over the
  authenticated request, at most 48 KiB. Sovite stores it through the existing
  content-addressed artifact store before the state transaction; the final
  transaction rechecks currentness before linking its key and digest. A stale
  upload can leave an unreferenced blob but cannot publish it or alter Job state.
- Runtime errors use the finite code set in `sovite/remote_execution.py`.
  Raw exceptions, stack traces, secrets, arbitrary output, SQL messages, and
  artifact contents are excluded from audit and journal projections.
- **P08 choice:** accepted outcomes are `completed`, `failed`,
  `retryable_failure`, and `cancelled`; the finite error-code set is enforced
  by the API. A completed result is a JSON object up to 48 KiB stored through
  the service's artifact store. `external_execution_uncertain` never releases
  content and becomes `blocked`.

### Lookup

- Lookup is scoped to authenticated workload and exact instance plus an
  operation identifier or exact execution tuple. It cannot use a partial job
  ID to reveal another scope or silently follow a newer fence.
- Lookup is an observation only. It does not claim a queued job, renew a lease,
  retry completion, or make a stale fence current.
- **P08 choice:** lookup key is `(workload_id, instance_id, operation_id)`.
  It returns a fixed metadata projection and leaves rows under existing owner
  retention. Missing rows return `not_found`; lookup performs no GC, retry,
  instance reuse, or tombstone transition.

## Bounded response and error data

- Every request field has an explicit byte or character ceiling; JSON
  parsing is strict and does not coerce values, collapse omitted and `null`,
  or accept unknown fields.
- Every response is checked against the existing 4 MiB inclusive owner limit
  before a journaled transition commits. The renew, handback, complete, status,
  and lookup projections have fixed fields and contain no caller-controlled
  strings except bounded identifiers and finite error codes. No truncation is
  permitted.
- Status and lookup return only a minimal projection. They do not return
  `task`, `resolved`, bearer capabilities, credentials, or unrestricted owner
  rows. Completion accepts a bounded sanitized runtime result and finite
  outcome only.
- Errors use a finite code/status map and bounded machine-readable body. The
  implementation uses the existing `{detail:{code,message}}` PlatformError
  envelope and preserves typed owner outcomes.
  Current authority/lineage outcomes must preserve their actual owner meaning.
  No raw exception text, SQL state, task/recipe, credential, or arbitrary
  stored content is serialized. An unclassified result is an error, never idle.
- **P08 choice:** the global request middleware bounds bodies to
  `SOVITE_MAX_REQUEST_BYTES` plus framing overhead; result JSON is at most 48
  KiB. Typed errors use the existing `PlatformError` envelope and codes
  `invalid_request` (422), `execution_scope_denied` (403),
  `idempotency_conflict` (409), `execution_fenced` (409), `cancelled` (409),
  `owner_unavailable` (503), `execution_payload_too_large` (413), and
  `not_found` (404).

## Transaction boundaries

1. Authenticate and validate bounded request syntax before starting a mutation.
   Trusted principal scope is injected by the adapter.
2. For each mutating operation, open the existing Platform transaction/session
   and acquire the same authority, Job, and universal lease locks needed by its
   invariant. No route, worker, or helper independently commits or creates a
   second owner store.
3. Under lock, recheck the exact identity/fence tuple, current authorization,
   cancellation/inhibition, deadlines, phase, and idempotency key/digest as
   applicable. The final clock/currentness check occurs after lock acquisition.
4. Apply the logical transition, journal its sanitized result if journaling is
   selected, and append audit in the same transaction. A refusal or any
   validation/serialization failure leaves owner state unchanged. A success
   response is sent only after commit.
5. On connection loss after commit, retry/reconciliation follows the
   operation-specific rule above. It does not rerun an unjournaled mutation or
   replace an existing committed journal result. If commit status is unknown,
   return/record unknown to the caller where safely possible; never guess idle
   or success.
6. Status/lookup are read-only observations against the same authoritative
   rows and currentness rules. They do not use the operation journal and cannot
   acquire side effects as a convenience.

## Acceptance histories

The acceptance suite must drive the real Platform, database, lease owner,
authorization, journal, and audit against disposable SQLite and PostgreSQL.
It must not substitute a test state machine for owner rows. PostgreSQL locking
claims require witnessed lock contention and bounded waits; SQLite cannot
qualify those races. Each history starts from a valid positive setup and checks
state and audit before and after each action.

| History | Required observations |
| --- | --- |
| Renew exact current fence | Renew once; assert Job and universal lease agree on owner and lease expiry, while fence, generation, attempt, and fixed deadline do not change. For gateway, assert current authority deadline matches. |
| Renew response lost after commit | Commit renewal, suppress the response, then retry/reconcile under the decided identity rule. Assert no second extension, no duplicate transition/audit, and a stable result. |
| Renew versus revoke/expiry/reclaim | Arrange actual lock ordering for both sides. If revoke/expiry/reclaim wins, renewal cannot revive authority or alter replacement scheduling state. If renewal wins first, the later transition observes the committed result and applies its own rules. |
| Status scope and no effects | Query current, finished, foreign-workload, malformed, and superseded tuples. Assert bounded minimal projection or decided typed outcome, no existence oracle across workloads, and zero change to queue, attempt, lease, authority, journal policy, or audit. |
| Handback response lost and retried | Commit handback, suppress response, retry. Assert the original result is recoverable per decision and no second release, audit rewrite, or change to a subsequent fence occurs. |
| Handback versus recovery/reclaim | Exercise both lock orders with an actual barrier. Assert handback only changes its captured fence; recovery or reclaim creates/retains the correct successor state and late handback cannot release it. |
| Complete response lost and retried | Commit a valid terminal result and suppress response. Assert duplicate handling is stable, terminal state and audit are single, references remain bound to the original fence, and no capability or artifact effect is duplicated. |
| Complete versus cancel/revoke/reclaim | Exercise both lock orders. Assert policy-selected winner, no completion on a replacement fence, and uncertain external effect remains explicitly uncertain rather than being guessed successful. |
| Lookup lifecycle and isolation | Query known, unknown, expired, pruned (if ever admitted), and superseded journal/execution keys in same and foreign workload scopes. Assert no side effects, no latest-incarnation substitution, and bounded result under the decided retention policy. |
| Idempotency conflicts | Reuse operation identity with altered operation kind, body, instance, principal, job, generation, or fence. Assert conflict with no state/audit mutation; exact duplicate follows its operation-specific replay rule. |
| Bounds and error redaction | At each accepted byte boundary and one byte above, test UTF-8 bodies including multibyte text. Assert inclusive/exclusive rule, rollback on oversize, no truncation, and no secret, raw exception, SQL state, task, recipe, or artifact bytes in response/error/audit journal fields. |
| Commit failure and post-commit loss | Force a precommit failure and separately drop the response after a known commit. Assert precommit rollback leaves no transition/journal/audit; postcommit reconciliation reports the committed outcome without repeating it. |

The choices above are implemented on the P08 candidate. They still require
positive, negative, lock-order, response-loss, and boundary histories against
disposable owner databases before deployment or remote runtime adoption. This
source implementation does not itself qualify a generated client, credential
cutover, deployment, or live worker host.
