# Interrupted repository evidence

An interrupted native run can leave useful edits without a completed native
turn, a known final provider charge or a successful Work result. Recovery
captures those bytes as `repository-recovery-artifact`. It leaves the original
Job status, error, public result, attempt outcome, active slots and cost
reservations unchanged. A recovery artifact is never a repair predecessor.

The owner-defined repository extension 1.2 adds
`POST /api/repository-runs/v1/runs/{run_id}/recovery`. The generated contract
accepts only `workspace_id` and an exact `attempt_id`; the authenticated caller
must own the existing Job. It returns metadata and an exact `source_digest`,
with `publication_eligible: false` and `repair_eligible: false`. It returns no
candidate bytes, execution capability, fence or new deadline. Requests cannot
select an authorization purpose, model, endpoint or implementation.

Capture requires a terminal uncertain, failed or cancelled Job with no live
execution lease. It takes the existing attempt lock and reads that exact
durable packet, original mirror, base, seed and fence hash. The builtin native
driver writes a protected recovery context before its first guest effect; old
attempts lacking that context fail closed. The context binds the actual
started execution deadline, packet and descriptor. A new worker lease cannot
replace any of those facts. Recovery never invokes `seed-v1`, `exec-v1`, native
app-server or a model.
If a partition left a running Job with an expired lease, the existing lease
owner must first reconcile it to an uncertain terminal state; recovery does not
perform queue reconciliation or change that outcome.

Cloud separately owns the protected `recovery_until` retention bound. The
original execution deadline remains immutable. Only quiescence and capture are
available during retention, which cannot exceed one hour after execution; the
capture/import operation has a separate maximum 60-second deadline. Each
transport block and Git operation repeats current Sovite data authority and
the exact Cloud descriptor/inventory check. Cleanup remains available after
active Work revocation. No active Work check is substituted for current
Sovite grants, binding, generation, profile, caller or data authority.

The root endpoint seals one immutable export frame after quiescence. A lost
reply can return only those same sealed bytes after exact boot, identity and
zero-process checks. An incomplete intent without a valid seal remains unknown;
neither the native loop nor exporter is rerun. Sovite validates that frame in a
fresh quarantine using the fixed bounded Git runner. It stores the validated
JSON artifact under the original Job's existing ArtifactStore namespace and
appends immutable recovery evidence to the existing Job record. There is no
second execution ledger or queue. A lost host acknowledgement reconciles the
same stored digest.

An output approver can review that exact artifact and use the existing
`OutputApproval` contract with the returned `source_digest`. The narrow source
validator accepts only an owner-recorded recovery artifact, never an arbitrary
failed Job result. The source class is the higher of the actual repository
input class and grant output class. Existing approver, procedure, class and
destination checks govern release. The existing candidate-retention operation
then publishes those exact released bytes and records a distinct
`retained-recovery-candidate` reference. Fresh Workspace readers use their own
current membership and disclosure policy, including after producer expiry or
loss of disposable VM state. Unknown provider reservations remain held and
automatic repair remains refused.

This source does not install or qualify a VM profile. Unit integration uses
real owner Jobs, Git quarantine, output releases and Workspace PostgreSQL, with
an explicitly synthetic Cloud transport. The separate native interruption
probe exercises an actual disposable KVM guest without paid model calls. An
operational qualification must bind the exact installed Cloud/native/driver
bytes and prove the complete interruption, successor capture and reader history.
Filesystem and database work assumes the protected local service environment;
there is no atomic transaction across the execution, Cloud and Workspace owners.
