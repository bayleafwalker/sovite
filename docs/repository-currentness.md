# Installed repository work currentness

A grant owner can publish `repository_currentness_required: true` in its existing
Sovite policy. This declaration belongs to the active configuration generation.
A new plain job on that grant is refused even when optional repository runtime
composition or the currentness implementation is absent. Capability discovery
shows a missing required authorizer as unavailable; it makes no Work request.
Requests cannot set the declaration, choose a purpose, or select an authorizer.
The portable-work packet and repository submission wire remain unchanged.

The installed application supplies one fixed `CurrentnessAuthorizer` object to
`Platform(settings, repository_currentness_authorizer=guard)` or `create_app`.
A supplied Platform and explicit guard must be the same composition. The API's
embedded worker uses that Platform instance. A separately installed worker must
use the same fixed application factory. Stock workers without the hook refuse
active guarded jobs. Sovite does not import Sprintctl, choose a foreign SDK,
load a configured plugin, or create an additional authority store.

The synchronous object has three keyword-only methods:

- `admit(repository_run, task, caller, recipe, parent) -> dict`: select and
  validate stable opaque owner selectors from protected installed data using the
  actual admitted packet, caller, grant, generation, limits and intent key.
  `parent` is the actual owner-validated repair predecessor Job, or `None`.
- `bind_job(job, admission) -> dict`: bind that stable admission to the actual
  newly minted Job UUID and packet. The returned owner record is finite JSON,
  bounded to 16 KiB. It contains no credential or cached currentness timestamp.
- `check_job(job, binding, authorization_started_at) -> None`: perform fresh
  owner authorization for active use. Denial raises `CurrentnessError`; Boolean
  return values do not authorize. The monotonic start is ephemeral and shared
  across the outer authorization and recursive source checks. Implementations
  must bound all actual owner reads against one total budget and limit each read
  to the remaining time. Facts are never cached as ongoing authority.

The fixed AgentOps implementation owns protected spec selection, actual Work
READ/delegation, holder/run/lease/checkpoint/requirements validation and intent
key derivation. It must compare any persisted endpoint/selector against its
current protected profile before using it. The core does not interpret a Work
schema. The authorizer must reject missing or ambiguous protected specs and
must require each configured spec's grant declaration in current owner policy.

Sovite stores the opaque owner record only at
`Job.resolved.repository_run.project_currentness` inside this generic envelope:
`{schema_version: "sovite-repository-currentness/v1", job_id, principal_id,
packet_sha256, binding}`. Every active check verifies UUID, caller, grant,
generation and the digest reconstructed from the actual job goal and original
repository limits before calling the hook. A repair inherits the actual parent's
immutable owner tuple, while its own envelope binds the new UUID and exact child
packet. The existing repository owner still controls lineage, budget, candidate,
feedback and lease/fence admission. There is no new ledger or packet authority.

The default `authorize_existing_job` purpose is active. Lease reserve/start/
renew, worker effects, gateway permit admission/renewal/completion, native
attempt context and repair execution recheck Work currentness. The declaration
or an existing binding without the hook fails closed. Explicit owner denials
block dispatch without incrementing an attempt; unavailable owners remain
unavailable rather than being reported idle.

Trusted internal `purpose="observe"` call sites omit only the active Work veto.
They retain all existing current Sovite grant, caller, generation, qualification,
class, approver and data-owner checks. These sites are bounded receipt/history
observation, released result/source access, candidate export, repair predecessor
read/approval context, and failed/cancelled completion reporting. New repair
admission binds fresh current owner authority; existing data cannot restart old
work merely because its bytes remain readable. New result release and successful
model completion remain active checks.

Owner GET/list/cancel, exact idempotent submission reconciliation and cancellation/
handback/stop cleanup retain their existing authority. Ready Workspace recovery
uses the reader's current Workspace membership and class/destination policy;
it does not revive the producer's expired Work lease or require its executor.
Public request and resolved-record fields cannot choose `observe`.

`CurrentnessError` exposes only static codes/messages: unavailable (503), revoked,
binding invalid or required (409). Unexpected hook errors map to unavailable;
owner response bodies, endpoints and credentials are not relayed. Fresh owner
reads veto individual boundaries. They do not make the Work owner, Sovite DB,
VM controller and provider request one atomic cross-network transaction.
