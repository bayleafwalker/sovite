# Internal execution lease owner

Status: **Internal O2a owner implemented; source verification complete against the schema5 parent, integration with current main pending.**
Accepted parent is schema5 source `62ea16d0c67d42bfd189dfe0e4e3c1e0b434b39c`.
This unit supplies no private API, remote client, inference/native routing,
deployment, credential removal, finite production retention or full R4/S4
acceptance. Existing jobs/queue, Platform transactions/current authorization and
audit remain the only execution-state authority.

## Source boundary and interfaces

The eventual implementation is bounded to lease_service.py, authority.py and
worker.py. Tests are root-owned test_execution_leases.py plus additive existing
backend/authority tests. No DDL/schema6, settings, policy, adapters, storage,
API/auth/contracts, packaging or dependency change is admitted. The preparation
adds internal frozen ExecutionPrincipal(workload_id,compute_id) and
ExecutionContext(instance_id,operation_id,job_id,generation,fence), and only:

```
operation_request_digest(*, operation_kind, principal, request) -> str
execution_owner_key(*, workload_id, instance_id) -> str
LeaseService.bind_trusted_worker(*, principal, instance_id) -> None
LeaseService.reserve(*, principal, instance_id, operation_id) -> dict
LeaseService.start(*, principal, context) -> dict
```

The original preparation used five stubs and preserved the existing lifecycle.
The implementation now supplies these interfaces and coherently adapts the
trusted Worker lifecycle to the universal fence.
These Python values are internal owner interfaces, never handwritten external
Pydantic, JSON Schema or TypeSpec contracts. First private API waits for accepted
shared foundation plus real O2/F2 consumer qualification.

`bind_trusted_worker` is an internal local-adapter binding only. It derives the
owner from this LeaseService's existing full Worker.id and requires instance_id
to be its full SHA256hex64. Future API adapters never invoke it or accept a
lease_owner field. Unbound owner lease key is `execution:` plus SHA256 of compact,
sorted, UTF8 JSON {schema_version:sovite-worker-execution-owner/v1,workload_id,
instance_id}. This is74ASCII characters and preserves the complete scope in the
new lease row. Bound trusted Worker preserves its existing Job.lease_owner=self.id,
heartbeats and inspection semantics. Caller requests cannot choose compute,
workload, qualification, deadlines, role, caller or grant authority.

## Locked reserve and start

Use existing sessions.begin and authority lock; retain PG claim advisory lock,
operation scope/row and Job/lease locking. Do not connect/commit independently or
create another store/runner. Complete logical transition plus journal/audit must
commit before returning success. No implicit recovery or retention inside
reserve; maintenance is explicit before the observation/negative-test baseline.

Reserve first establishes available active owner state, including an empty
population. It performs one bounded existing eligible claim attempt in trusted
compute scope, checks current authorization and gateway binding enabled/inhibit
policy, and counts reservations toward model concurrency. Known403 grant denial
may block/audit that candidate and continue in the same transaction.503/currentness
or DB corruption/outage must propagate, never become idle. Gateway binding
admission reuses existing policy without requiring an online helper or allowing
a direct fallback.

Reserve chooses one opaque UUID4hex32 fence for every model kind and sets running
and Job lease owner/until plus WorkerExecutionLease phase=reserved. It does not
increment attempts, set started_at, create capability or set execution_deadline.
First start locks/rechecks exact workload/instance/job/generation/fence, live
reservation/cancellation/current authority and binding. It increments once,
preserves the first started_at convention, sets phase=started and a fixed all-job
deadline=min(grant timeout,server timeout)+owner now. Later lease renewal cannot
reset that deadline. Gateway issue_execution must consume this exact owner
fence, actual workload, owner timestamp and deadline, never generate another
execution epoch. Only first committed start returns a raw bearer, which remains
transient and is stored solely as its existing digest.

The actual running Worker closes over its claimed Job's immutable universal
fence and passes it to renewal/handback/finalization; it must never borrow a new
execution_fences[job_id] entry after same-instance reclaim. Existing lifecycle
methods maintain Job/new lease/ExecutionAuthority atomically: reserved expiry
returns to eligibility without attempt charge; started and legacy-no-lease rows
retain conservative safe-retry versus execution_uncertain semantics; handback,
finish and recovery mark the old phase finished and clear scheduling ownership.
Gateway refresh failure cannot be reported as successful lease extension or
revive revoked capability. Nongateway jobs need no inference authority row.

## Exact private results and journal

Idle is exactly {state:idle}. Reserved response is exactly {state:reserved,job},
where job has job_id,generation,fence,lease_until,task,resolved. Task and the full
existing resolved recipe are deep copied, preserving its versioned nested
policy objects. This is an internal private view; a minimal external execution
projection still needs separate root authoring before F2/API admission.

First start has exactly state=started,job_id,generation,fence,lease_until,deadline;
only gateway adds execution={capability,context}, where context is the existing
execution_context for this Job and actual workload. Current replay has exactly
state=already_started,job_id,generation,fence: execution is absent, never null,
empty or reminted. Same/different operation IDs cannot consume another attempt
or issue another bearer for a started fence. Lost first response leaves a future
remote client stopped; reconciliation is not permission to relaunch.

Enforce4MiB compact canonical UTF8 JSON bytes over the entire reserved response
before any reservation/journal/audit commit. No character-count substitute or
truncation. execution_payload_too_large413 leaves the job queued and state
unchanged. Task submission retains its existing independent max_request_bytes.

Operation PK is exactly workload+instance+operation_id. New request digest binds
schema_version=sovite-worker-execution-operation/v1,operation_kind, trusted
workload_id/compute_id and exact request including instance/operation ID. Digest
is bare64hex over sorted compact ensure_ascii=False valid finite UTF8 JSON;
no coercion/defaults, omitted/null collapse or historical authored/audit/connector
canonicalizer change. Pull/start vectors are independently specified in tests:
6def1742f65e2c9554e465cf74a65a1fa49946291a2448b9e5f917276167e90b and
2b495125d31748ff9903c6d95a098a170f4b3cde7bab5688ee87a435a4f9b663.

Sanitized reserved journal result has state,job_id,generation,fence,lease_until,
task_sha256,resolved_sha256. Task hash is exact raw UTF8 SHA256; resolved hash is
finite compact canonical JSON SHA256. Idle stores only state. Start stores only
state/job/generation/fence/lease/deadline metadata, never execution, raw bearer,
credentials, task, recipe or artifact. Do not overwrite old committed results.
Live reserved replay rechecks phase=reserved, expiry, same current fence, scope,
authority and private hashes, then reconstructs from Job; otherwise execution_fenced
without claiming another job. Started replay checks currentness and returns only
already_started; stale/expired/reclaimed/revoked contexts refuse.

No operation/lease GC in O2a. Indefinite deduplication is source behavior only;
finite capacity, instance identity lifetime and durable tombstone/epoch policy
are explicit blockers before remote/runtime adoption. Do not apply existing
inference-history7day sweep to worker journals or silently permit key reuse.

## Safe internal errors and oracle boundaries

Proposed frozen owner statuses used by tests: invalid_request422;
execution_scope_denied403 for unknown/foreign scope; execution_fenced409 for
stale exact context; idempotency_conflict409 for changed-body/kind/compute reuse;
execution_payload_too_large413; cancelled409; binding_inhibited403; existing
current-authority grant/qualification403 and not_initialized503; owner_unavailable503
for unavailable/corrupt owner rows, including invalid phase/nonfinite deadlines.
Preserve actual owner-context statuses, not a code-only
mapping borrowed from operator activation. No arbitrary private input or exception
content enters error text. The extracted38 RuntimeFailure literals and only
upstream_unavailable/retryable=true are frozen for later completion; O2a creates
no wire failure enum or remote complete operation.

Independent oracles use the real Platform/Database/LeaseService and actual owner
rows/audit, not a test state machine. They cover schema5/submit/authorization
setup; reserve/no attempt; first start; reopened same-key and different-key
replay; raw bearer absence; guarded private byte reconstruction; current authority,
binding and real empty-owner versus outage; full valid policy response over byte
cap; actual audit/precommit rollback and postcommit loss; reserved recovery and
new fence; malformed/foreign scope; trusted local identity and same-Worker stale
execution; PG witnessed lock races for one job/model concurrency, start-once and both
orders of actual cancellation/inhibition versus start.

The oversize oracle compiles/activates a genuine valid programmatic policy with
100 selected tools whose16000-character names/descriptions satisfy current owner
schemas, yet whose complete UTF8 response exceeds4MiB. It does not inject an
invalid stored recipe whose unrelated validation failure could mask the cap.
Controlled GrantState and private Job byte mutations are labelled derived owner-row
negatives; they isolate individual guards after positive setup and do not claim
historical or field producer truth.

PG races must observe actual pg_locks contention before release, with bounded
waits and finally cleanup. SQLite checks cannot stand in for PG row/advisory lock
qualification. Root runs configured actual-PG selections with zero skips; missing
schema/tool/backend setup is not a semantic mutant kill. Existing current/legacy
backend, migration, authority and inference tests remain required regressions.
Root separately freezes/names independent reserve-attempt, start-once/fence,
current-authority, bearer-replay and rollback mutants after complete valid setup.

Remaining remote renew/status/handback/complete, bounded artifact readback plus
relock/currentness, generated contracts/private API/client, native/gateway routing,
network/credential cutover, finite deployment retention and full R1–8/S1–6 outcomes
remain separate. No O2a source result waives them or changes live authority.

## Root preworker correction amendment

The internal ABI above is adopted: gateway execution exactly capability/context,
adapter-only bind_trusted_worker, raw UTF8 task hash and finite canonical resolved
JSON hash. Independently pinned task vector naïve — β has13UTF8bytes and SHA256
`a19aad37b3b79391fd7818f8eb049d7c586721448e5c0bdd37938aa35c89975a`.
The accepted direct/gateway private recipe vectors are respectively
`3b7ea048eba615fd8b0832d7d2d828054ee9f53565010796226f91c76e06734c` and
`60f69c3279fe7d248cd3234248d272bc733d30aca5465b789de81f1e5e0cb9d0`.
Equivalent dictionary key order keeps the canonical hash; changing original
Unicode normalization bytes must not silently keep the task hash.

Current authority is independently tested while still reserved, before FIRST
start, and after start for replay. GrantState-only fault fixtures deliberately
leave cancellation false. Actual inhibit_binding similarly leaves Job cancellation
false; disabled/inhibited queued gateway candidates are never dispatched and a
following eligible candidate must still be selected. Actual public cancel/inhibit
races remain separate from a PG GrantState-only writer that isolates the grant
predicate under the same witnessed lock barrier.

Normal trusted renewal must report true only when both Job and universal lease
extend coherently, with unchanged fixed execution deadline/fence/scope and matching
authority deadline for gateway jobs. Digest withdrawal, expiry or replacement
fence must report false and extend neither scheduling copy or revive capability.
Actual old-task authorization_check and shutdown handback must use the captured
old fence even after same-instance reclaim; they cannot modify the replacement.

Root admits exactly two existing functions in test_pr3_review.py for correction.
The withdrawn-capability test replaces the unsafe successful-renew expectation
with false, preserving no-resurrection and adding both scheduling-copy checks.
The60-second refresh/clamp test preserves its original success/clamp assertions,
but its owned synthetic shorter ORIGINAL-deadline fixture sets the corresponding
universal lease execution_deadline and authority deadline in the same transaction,
and requires the universal row to exist. This is not permission for production
transitions to shorten/extend/change a fixed deadline. All other PR3 functions
remain untouched; their HTTP/gateway execution remains root-owned.

The whole-response oracle now separately proves4194304bytes exactly is admissible
and4194305bytes refuses. Each uses a genuine compiled/activated recipe individually
below4194304bytes and a genuine accepted60000byte task below its65536byte request
cap. Valid policy display fields supply bounded UTF8 padding; required response
metadata is included independently. Setup controls prove these premises before
reserve exists. The expanded recipe-over-cap control remains supporting evidence;
it cannot alone qualify the whole-response boundary.

## Verified source boundary

The internal source passed1353 tests against disposable PostgreSQL17.11 with
no skips, including actual lock-wait barriers, legacy owner/API regressions and
additive deadline, authority, replay and final-row-lock clock controls. Four
separate copied-source mutants (whole-response cap, finish deadline, current
binding at publication and final-lock time) reached their intended failures
after eleven genuine PostgreSQL positive controls. The isolated database was
removed. This is source qualification, not hosted/remote or runtime adoption.

Current-main integration must preserve its workflow-aware existing-job
reauthorization and current attempt/output behavior. Actual integrated tests
and main CI are required before landing. Finite operation journal capacity,
instance lifecycle and tombstone/epoch policy remain blockers before remote
adoption. No API, gateway transport, credential/network or deployment change
is supplied by this unit.
