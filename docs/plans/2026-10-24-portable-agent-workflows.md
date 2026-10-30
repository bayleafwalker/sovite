---
doc_id: portable-agent-workflows.sovite.v1
status: proposed
supersedes: []
---

# Repository-editing sessions on enrolled execution hosts

Status: investigated implementation plan, 2026-10-24. Source surveyed at
`b712d5f69a62e618e66acb6a6beae9dad5e25e56` (canonical main).
Cross-repository dependencies, PW package definitions and qualification scenarios
are in the [programme plan](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-10-24-portable-agent-workflows.md).
Documentation landing does not certify implementation or deployment.

## Current boundary and decision

Sovite already owns durable work execution, leases, fencing, renewals and native
harness bindings. `sovite/worker.py` and `sovite/lease_service.py` implement that
execution path. Native answer-producing launchers do not yet constitute a
qualified repository-editing session. `docs/PLATFORM-SPEC.md`,
`docs/OPERATIONS.md` and `docs/CALLER-DISCOVERY.md` describe the relevant limits.
The inference-only `sovite/connector/protocol.py` (`sovite-inference.v1`) is not
an execution-host enrollment protocol. Installed declarations and discovery are
not live capacity or provider-auth evidence.

Extend the existing Sovite execution plane and host-side native bindings for
repository work. Reuse execution-owner R4/F2 contracts and released shared schema
ownership; do not add a second queue, put a runner in Vuoro, or turn the inference
connector into a reverse command channel. Keep a possible package extraction
as a later implementation decision. This plan does not enable remote execution.

As of the 2026-10-25 workspace-foundation decision, an internal pre-launch
utility validates an owner-resolved repository-session packet, prepares an
exact-base detached worktree, and captures candidate evidence. It is not
connected to job admission, the execution owner API, a worker, or a provider
launcher. The repository-session API and launch capability remain unreleased;
see the [workspace foundation decision record](2026-10-25-repository-session-workspace-foundation.md).

## Work packages

- **SV-PW1 / contract and qualification:** shape a repository-session capability
  against PW-01 and the execution-owner API work (agentops#2850). Identify the
  exact implemented versus missing R4/F2 operations. Specify authority, source
  base, scoped worktree, permitted surfaces, input/output access class, native
  session handle, attempts, deadlines and spending/concurrency limits. Candidate
  output is an immutable ref plus evidence, never an accepted work transition.
- **SV-PW2 / native session binding:** check out a verified canonical or mirror
  commit into an isolated worktree, use a pinned native CLI/SDK with explicit
  headless settings and observed model/configuration, support tools required for
  editing, and emit a worker-origin start receipt. Preserve native planning,
  compaction, goal and continuation behavior where qualified; do not approximate
  it with repeated unbounded prompts. Add a provider capability matrix and an
  honest unsupported/manual result for unavailable cloud launch surfaces.
- **SV-PW3 / recoverable attempts:** persist launch intent before invocation,
  capture the provider handle, and reconcile lost replies before retrying. Reuse
  the captured execution fence for renewal, cancellation and output publication.
  A local journal is a cache of owner-correlated facts. Expired/replaced attempts
  may retain evidence but cannot publish as the current attempt. A work
  reservation is not this execution lease.
- **SV-PW4 / enrolled hosts:** integrate authenticated enrollment, revocation and
  per-pool capacity through the execution-owner boundary. Separate control-plane
  trust, host identity, repository grants and inference-helper credentials.
  Start with one qualified host class; heterogeneous/cloud pools follow the same
  tests. No generic reconciler or automatic fleet support is claimed today.
- **SV-PW5 / receipts and qualification:** emit useful progress, lifecycle,
  heartbeat and usage-coverage observations through owner APIs; produce candidate
  refs and exact-head tests for downstream verification/landing. Demonstrate
  restart and migration in the pilot before advertising unattended operation.

The 2026-10-30 requirements below refine SV-PW1/2/3/5. Implement them through
the existing owner operations and a versioned repository execution profile;
they do not change the current generated `agent-http-v1` wire or enable a
mutable host.

SV-PW1 depends on PW-01 review; SV-PW2/3 depend on host isolation PW-02.
SV-PW4 consumes broker/appservice commissioning and vuoro-cloud composition;
versioned releases must let those consumers pin a qualified capability.

## Route and effect safety

The native caller selects permitted model preferences and spending. Sovite runs
the exact authorized resolved binding; it does not silently switch family on
failure. A permitted switch creates a linked attempt after reconciliation.
Rate limit, revoked auth, policy refusal, exhausted budget, capacity unknown and
ambiguous execution are separate outcomes. Prior attempt lineage does not grant
artifact reuse: preserve explicit source release/transfer authorization.
Do not replay an in-flight or uncertain provider effect because a CLI printed
nothing. Cancellation/takeover must address the exact process/session, not a
name search that can target a sibling.

## 2026-10-30: capture, ownership and recovery requirements

Source baseline: `5a57f75baefbeefeba02eadaec5b324bbb003f31`. The internal
repository-run seam and candidate export helper exist, but no installed mutable
executor is established by them. Current renewal checks authority, fence and
deadline; progress-sensitive renewal and the receipt requirements in this
section are implementation work. Keep that distinction in capability discovery
and release notes.

### SV-PW1/3: distinguish delivery, acceptance and effects

| Observation | Meaning and owner |
| --- | --- |
| Dispatch delivered or locally read | Transport/capture evidence only; no execution acceptance. |
| Reserved and started | Sovite accepted the exact current binding and execution fence through its existing owner transitions. |
| Process exited or result submitted | Runtime outcome; it may leave effects unresolved and does not settle the work owner. |
| Effect receipt | A destination or qualified adapter observed the named effect; retain the observation basis and unresolved outcomes. |
| Result released | Sovite authorized the exact content for the named reader/destination under its current output contract. |
| Work accepted | The work owner's acceptance decision against its own evidence and conditions. |

Before start, check the enrolled host's qualified capability, authorized binding,
repository/base availability, provider-auth observations and capacity. Reserve
capacity under the owner transition; an old presence dot or installed CLI is
not a capacity reservation. Record unsupported or unknown provider observations
explicitly and apply the installed policy to them. These observations cannot
grant repository access or substitute for current admission. Hosted callers
request work; the LAN-side owner grants local effects. Keep the administrative
lane separate from the workload interface.

A supervisor may stop renewing when qualified progress evidence becomes stale,
but must distinguish idle, a bounded active tool operation, lost observation and
an exited process. A tool-start observation has an immutable operation deadline
bounded by the job deadline; a living shell, repeated status text or another
heartbeat cannot extend it indefinitely. Use adapter observations, with source
and observation times, rather than an agent's assertion of progress. The owner
evaluates freshness on its clock and rejects stale/reordered observations.
This needs a generated profile/operation revision and race qualification before
changing renewal behavior; do not append fields to the closed current wire.

Expiry removes publication/renewal authority; it does not prove the old process
stopped. Reconcile or cancel the exact predecessor before a new attempt can
repeat an effectful operation. A stale worker may preserve private evidence but
cannot publish under the successor's fence. A lost acknowledgement or absent
lookup row remains unknown; safe read-only/idempotent recovery retains the
existing owner rules.

The execution API's `(workload_id, instance_id, operation_id)` remains the
identity of one owner mutation. An external effect needs its own stable intent
and destination operation key across retries, plus a new attempt reference for
each execution. Do not use a lease fence or a new transport operation ID as the
effect's deduplication key. Destination adapters must qualify any conditional
mutation, idempotency or receipt-reconciliation guarantee they claim; an inbox
or owner journal cannot make an arbitrary external effect atomic. Record the
intent before invocation and preserve `unresolved` when the reply is lost.

### SV-PW2/5: native adapters and owner-correlated evidence

Keep the adapter surface small: launch, attach, observe, interact, collect,
cancel and provider-native resume where qualified. Publish support per harness
and version, including account mode, repository capability and the exact resume
prerequisites. Distinguish resuming a native session from starting a successor
with a portable handoff. Missing native state, credentials or repository bytes
must yield an explicit unavailable/continuation outcome. Never turn that into
an unrecorded fresh session or a claim that a cross-harness resume succeeded.

Candidate/evidence capture must link these facts without redefining the owners:

| Evidence | Required interpretation |
| --- | --- |
| Run and attempt | Existing work/run reference, predecessor, job, generation and private execution-context binding; public projections omit the fence and credentials. |
| Runtime segment | Requested and observed harness/provider/model/account mode, pinned adapter revision, start/end observations, switch reason and uncertainty. A new route/host is a new linked attempt where existing authority requires it. |
| Capture | AgentOps capture reference, exact immutable native-record range, collector coverage/gaps, and semantic session-note reference. Absence of a native log is an explicit limitation. |
| Effects | Links to intent and destination/adapter receipts. Keep declared intent, denied-before-invocation, observed result and unresolved effect distinct. Agent prose is an attributed claim. |
| Usage | Per-segment observed/estimated/unavailable usage and billing basis, including subscription/quota events. Missing charges are unknown, not zero; reconcile overages and stop further paid work under the current budget contract. |

The capsule is an evidence member, not a new authority object. AgentOps owns its
[capture and export profile](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-10-30-capture-integrity-and-export.md);
Sovite owns execution and release facts. Preserve native logs as native bytes
and expose only the portable envelope/approved projection. The historical
`session-capsule/v1` and `session-note/v1` contracts stay unchanged; this plan
does not emit or admit `session-capsule/v2`. Mechanical capture does not contain
the semantic handoff: the session note states what a successor can act on and
which claims remain unverified. A collector hash chain verifies retained
linkage, not that the provider emitted no omitted records or that an unseen
crashed tail was complete.
An unavailable native log may still permit a metadata/note handoff under its
own output contract, with that coverage limitation declared. It cannot claim
verified full-transcript lineage.

### SV-PW5: classify and release the exact export

Raw native records remain in their admitted private storage. A redacted export
is a separate immutable artifact with its own digest, source lineage and a
versioned transformation/scan record. Retain only safe rule identifiers/counts
and outcomes in shared scan evidence; do not copy matched secrets into findings.
Apply the declared policy to the complete export, including notes, attachment
manifests, effect/usage metadata and references. A local raw reference alone
does not grant the recipient a read or make its contents eligible for egress.

Scanning must precede egress, but a clean scan or `scanned_before_egress` boolean
is not a release decision. Reuse the installed output contract: bind the exact
source and export digests, contract/policy revision, authorized destination,
reader and approval/decision evidence, and recheck current authority when
releasing. Changing bytes or destination invalidates that decision. Scanner
failure or incomplete coverage remains visible and cannot silently become a
clean result. This uses existing authorized approver procedures; it adds no
mandatory human round trip for each run.

The existing [candidate export seam](2026-10-29-repository-candidate-export-seam.md)
provides an exact-byte precedent but currently accepts only bounded UTF-8
content and has no installed export service. Arbitrary transcript/binary export
needs its own qualified profile. Do not widen that helper's authority or invent
a generic upload path to make a capsule transferable.

Typed classifiers may advise triage or choose among already eligible execution
options. Their version, calibration/evaluation basis, confidence and abstention
are evidence; neither a label nor parse-valid output grants an effect or lowers
an input class. A bounded deterministic repair search may produce a candidate
before an LLM call, under the same repository scope and frozen verifier. The
provider controller owns that repair policy; Sovite bounds its execution and
records resource/attempt outcomes, including unsuccessful search.

### Qualification increments and recovery

Keep these within the existing work packages, in this order:

1. **SV-PW1:** freeze adapter capabilities and evidence references against the
   AgentOps capture contract and the shared execution-contract owner. Preserve
   old evidence as unknown where it lacks coverage or usage; do not backfill
   completeness or infer a successful provider resume.
2. **SV-PW2/3:** qualify one native host using actual owner operations. Cover an
   idle-but-alive worker, a legitimate long tool call within its cap, a call
   exceeding its cap, reordered progress, disconnection while execution
   continues, expiry with the old process alive, and loss of an effect reply.
   Assert no stale publication, no duplicate effect and bounded reconciliation.
3. **SV-PW5:** capture an interrupted/truncated session, a provider/quota switch
   and unknown cost; verify exact source/export digests, authorized destination
   and secret-free projections. Mutated bytes, changed recipient, failed scan,
   missing bytes needed to verify the asserted lineage and revoked release
   authority must not obtain a successful export. Reconstruct the handoff on a
   clean host from retained artifacts;
   inspect the note/envelope without raw-log access and retain declared unknowns.
4. Publish the qualified capability and pin it in consumers before enabling
   one host/repository. Expand only after its failure histories pass. Rolling
   back disables new starts on that profile, drains or reconciles in-flight
   attempts and retains journals/artifacts; it never erases fences, unknown
   effects or old evidence to make an older consumer appear compatible.

## Acceptance and implementation gates

Run existing queue/lease/connector tests and state-protocol verification for
changed risk surfaces. Add meaningful histories for duplicate submit, launch
reply loss, host restart, lease expiry, takeover with old process alive, unknown
provider handle, canceled edit and revoked repository grant. Require clean-host
editing, dependency-aware replanning, native resume, denied coordinator/sibling
file reads, and exact candidate provenance. Test aggregate concurrency/budget
exposure and missing usage coverage. Use disposable state; source success is
not live enrollment or deployment evidence. Roll out one host/repository before
the cross-host Mars Founder plus second-repository demonstration.
