# Worker remote boundary status

Status: **No supported remote execution-owner or artifact-write API exists in
this source. The Compose worker therefore still requires SQL and S3 access.**
This note records a source boundary, not a deployment or qualification claim.

## API admission gate

The internal owner plan, [`2026-10-23-execution-lease-owner.md`](2026-10-23-execution-lease-owner.md),
states that O2a supplies no private API and that the first private API waits for
the accepted shared foundation plus real O2/F2 consumer qualification. It also
records the internal owner candidate's canonical landing and CI as pending. This
producer source has the internal Python `ExecutionPrincipal`, `ExecutionContext`,
`LeaseService.reserve`, and `LeaseService.start` interfaces and owner tests, but
no accepted external F2 request/status schema, generated worker contract/client,
or evidence that the shared API foundation and consumer gates passed. The API
contract document contains no worker execution route; the only current
`/internal/authority/*` surface is for inference gateway authority.

The programme queue authorizes continuing this API work after its ordered
contract prerequisites. It does not supply the missing external fields,
operation-journal semantics, or consumer qualification evidence. Adding routes
now would turn internal private owner views into an unratified network contract.
The route-absence regression in `tests/test_worker_remote_boundary.py` keeps
this gate visible until that evidence is attached and the external contract is
admitted.

## Current source path

`Worker.claim`, renewal, shutdown handback, recovery, and finalization call the
owner-side `LeaseService` against `Platform.db.sessions`. The lease service's
own contract calls it an internal extraction that cannot run without database
authority. Its state transitions lock and update job, lease, execution
capability, operation journal, and audit rows in owner transactions.

There is no `/internal/execution/*` or other worker lease API in `api.py`.
`/internal/authority/*` is a separate gateway inference-authority surface and
does not implement worker claim/start/renew/status/handback/complete/lookup.
`control_client.py` only implements hosted bundle compile and activation. It is
not a worker client.

`Worker.execute` calls `Platform.store.put` directly. It persists the private
object before the fenced SQL finalization transaction records the object
reference, release projection, and audit. S3 and SQL have no shared transaction.
The current worker consequently needs both database and object-store
credentials in Compose and Helm. Native harness composition is separate and has
no database or object-store credentials.

## Next implementation seam

The next safe API slice, after the admission gate above, needs an explicit,
authenticated owner API and client
for the existing owner methods. It must define a dedicated workload identity,
bounded request and response schemas, operation journal/idempotency behavior,
response-loss outcomes, and exact projections while preserving the current
transaction boundaries, owner-generated fences, immutable start context,
revocation checks, and no-replay rules. The model gateway's authority token is
not worker authentication and must not be reused for this purpose.

Artifact output needs its own boundary. An owner-mediated write or a narrowly
scoped, one-use upload grant must bind the object to the current job and fence,
preserve digest verification, and leave final release authorization in the
owner transaction. Until those contracts are admitted and implemented, removing
worker SQL or S3 credentials would break execution or move authority into the
worker. Do not mask these environment variables out of the current composition.

No production migration, rollout, or cluster observation follows from this
source review.
