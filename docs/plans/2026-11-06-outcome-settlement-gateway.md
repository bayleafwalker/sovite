---
doc_id: sovite-outcome-settlement-gateway-2026-11-06
title: A bounded outcome-settlement gateway proof
purpose: proposal
lifecycle: proposed
effective: 2026-11-06
subjects: [execution, authority, evidence]
related: [vuoro-outcome-settlement-proposal-2026-11-06]
---

# A bounded outcome-settlement gateway proof

## Purpose and status

Extend the existing resource-owner gateway with one operation whose effects can
be established at the target after an interrupted response. The first proof is
publication of one analytics partition in an isolated lab. It must demonstrate
useful delegated work, current resource authority, recovery and independently
decided Work acceptance in the same workflow.

This is an implementation proposal within the
[Vuoro outcome-settlement plan](https://github.com/bayleafwalker/vuoro/blob/main/docs/plans/2026-11-06-outcome-settlement-proposal.md).
Landing this document records the requested planning work. It does not install
an operation, grant access, qualify a deployment or change hosted-service and
workspace commitments. Commercial positioning remains a hypothesis. The
[AgentOps qualification plan](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-11-06-outcome-settlement-qualification.md)
owns the cross-repo exercise; the
[Sprintctl owner plan](https://github.com/bayleafwalker/sprintctl/blob/main/docs/plans/2026-11-06-outcome-settlement-owner.md)
owns Work decisions.

Source baseline inspected on 2026-11-06:
`db7487fe8beb22268bed4d53fdebefaaabeb76c0`. Source, release, installation and
qualification are separate evidence states throughout this plan.

## Reuse the current owner path

The [v2 resource contract](../contracts/resource-owner-v2.md) already contains
closed model-peer health and KVM lab create/observe/stop operations. The gateway
has resource-owned policy, durable local admission and retained observation
bytes. An admitted operation without a retained observation remains UNKNOWN;
delivery retries do not repeat its effect. The
[pod gateway source slice](2026-10-29-resource-gateway-service.md) describes the
earlier boundary. Neither document establishes a commissioned installation.

| Responsibility | Existing owner | Increment required by this proof |
| --- | --- | --- |
| Work identity, revisions, verification basis and acceptance | Sprintctl | Reference the exact operation and target evidence; retain unresolved effects and stale reports without treating them as acceptance. |
| Caller, configuration, execution attempt, lease and output admission | Sovite execution owner | Bind the proposed operation to the actual admitted attempt and a stable business operation reference; recheck current authority. |
| Resource policy, target credentials and invocation | Resource-owner gateway | Add one closed publication adapter and an explicitly installed target binding. No arbitrary SQL, shell command, URL, path or credential enters a worker request. |
| Publication, fence and effect journal | Selected lab target | Enforce publication preconditions and retain the effect receipt in the same target transaction. This is new target integration work. |
| Verification procedure and acceptance policy | Customer-installed verifier and Work owner | Verify the exact source/candidate/target tuple; decide acceptance under current customer policy. |
| Request/status presentation | Vuoro composition and Cloud | Expose authorized references and bounded status; do not acquire local resource credentials or acceptance authority. |

The existing resource request identifier derives from the actual job and its
operation ID. It is not yet a business-operation deduplication guarantee across
replacement jobs. Preserve that attempt-specific protocol; add an explicit
mapping to stable business intent instead of changing the meaning of old IDs.
The gateway's local SQLite store is operational state, not a second Work ledger
or a transaction shared with the destination.

Native harness launch and workspace capabilities remain independently
qualified. In particular, the [repository-session foundation](2026-10-25-repository-session-workspace-foundation.md)
is not evidence of a released repository-session API or worker launch. The
[private task receipt integration](../integrations/vuoro-private-workflow.md)
does not qualify protected target writes. This proof can first use a bounded
native caller against a declared supported interface; it must record the
actual launch, attach, observe, collect and resume capabilities exercised.

## One reference operation

The proposed operation publishes a prepared, immutable candidate for one named
partition. A deterministic transformation produces that candidate from a named
source snapshot. The installed verifier checks the source and transformation
revisions, schema and exact expected content for the lab fixture. A row count
or an agent's explanation alone does not establish correctness.

The initial destination is a disposable PostgreSQL target with a publication
pointer and operation journal in the same transaction boundary. Candidate
preparation has no authority to change the active pointer. Consumers must read
through that pointer; otherwise this operation cannot claim to control what
they observe. Staging cleanup is a separate bounded maintenance action.

The resource owner installs the target, partition scope, adapter and verifier
digests, data/output classes, destination, permitted transitions and resource
limits. Requests carry logical selectors and immutable references. Only the
gateway's constrained target role can invoke the publication procedure for the
selected agent operation; workers and model/tool subprocesses cannot use it
directly. Administrative and other legitimate target writers remain separate
authorities and must participate in the target revision protocol.

The immutable business intent binds at least:

- stable business operation reference, target installation/incarnation, retained
  installed-target configuration revision and partition;
- source snapshot, transformation revision and candidate digest;
- expected current publication revision and desired revision;
- verification profile/version and verifier digest.

Each admission separately binds that intent to the actual execution attempt,
current policy and output-contract revisions, target-enforced authority epoch,
recipient/destination and evidence retention horizon. A new attempt or renewed
permission does not change the business intent digest. It also does not inherit
the old attempt's permission. A Work currentness response is a snapshot and
retains `authorizes_effect: false`; the resource owner supplies effect authority.

Preserve v2's unchanged-target-configuration rule for retained follow-ups.
Rebinding a logical target alias to a fresh database cannot redirect an old
operation or turn not-found there into evidence about the original effect.
Reconcile against the original installation/incarnation and configuration;
if it is unavailable, keep UNKNOWN. A replacement or changed configuration
requires an explicit new qualification and authority binding.

These are contract requirements, not a new published schema. Design the closed
wire extension in the owning contract source and regenerate its SDK. Keep v1
and v2 behavior compatible; do not silently widen their operation unions.

### Commit at the target

The publication procedure must serialize changes for the selected partition
and, within one database transaction:

1. Authenticate and authorize the caller for the requested operation or historical
   lookup, and validate the exact intent digest.
2. Check the operation journal. The same operation with changed content is a
   conflict. An identical intent under a different transport or attempt ID
   resolves to the retained operation or is refused; it cannot publish twice.
   Return an existing result without applying a new-write revision precondition
   to its historical receipt. Lookup permission does not grant permission to write.
3. For a new publication, validate the expected target revision and current target
   authority epoch/expiry, then switch the active pointer to the verified immutable
   candidate and append a
   receipt containing the prior/new revisions and intent digest.
4. Commit both changes together, or neither. A gateway-side success record
   alone is not this receipt.

A remote authority check performed just before a write is insufficient to
establish this target transaction boundary.

The business operation reference is persisted before dispatch and survives
attempt replacement. The target also enforces uniqueness over the canonical
intent for the partition and expected revision, so inventing a second request
ID cannot evade it. A changed source, candidate, transformation or expected
target revision is a revised intent requiring a fresh authorization decision.
If the operation was subsequently reversed, its original receipt still means
that it applied historically. Return that receipt and the current target state
separately; a duplicate request must not silently republish the reversed effect.

The target guard and publication transaction must serialize epoch changes with
effects. Upstream revocation and lease changes still cross a network: record
when inhibition is requested and when the target acknowledges its effective
fence. Do not claim instantaneous cross-network revocation or that a later
revocation undoes a previously committed effect. If current authority cannot be
established, new effects stop; already admitted effects require reconciliation.

## Recovery and outcome decisions

Authorization to attempt an effect, evidence of that effect, and acceptance of
the Work are distinct decisions. Customer-installed standing policies may
automate authorization and acceptance. Adding a mandatory person at every
action is not a requirement. A performer cannot manufacture the accepting
identity or replace the installed verifier to make its own result pass.

| Observation | Required behavior |
| --- | --- |
| Rejected before target admission | Retain the refusal and its basis; no effect is claimed. |
| Exact target receipt and matching publication observed | Retain target acknowledgment and verification evidence. Submit them to the Work owner for an independent acceptance decision. |
| Target committed, response lost | A successor with fresh authority queries the journal using the same business operation reference. Reconstruct the exact committed result without invoking publication again. |
| Gateway admission exists, but the target outcome cannot be established | Keep UNKNOWN. Do not turn a timeout, an unavailable replica or an unqualified not-found result into proof of no effect. |
| Strong target read proves the operation absent | Before any retry, establish that no admitted or delayed request can still commit under the old epoch. Only a fresh authorized retry of the same intent may then proceed. |
| A superseded attempt reports success | Keep the attributed report and any valid target evidence. It cannot accept Work, revive old authority or settle the successor's attempt. |
| Target changed again after publication | Preserve the original receipt and record the later revision separately. Decide current Work acceptance against its pinned profile; do not rewrite history. |

A known publication does not prove the requested business outcome. The verifier
must test the acceptance profile against the exact candidate and relevant target
state. A failed verification leaves the effect recorded and Work unaccepted;
it does not erase the effect or authorize rollback. UNKNOWN likewise does not
finish a Job, settle cost or accept Work by itself.

### Restore and reversal

Follow the existing [restore discipline](../OPERATIONS.md): restore with writers
stopped and grants inhibited. A restored gateway or owner database cannot be
trusted to know the newest target effects. Reconcile against the target journal,
establish a fresh non-reused authority epoch, and check current configuration
before any publication is enabled. The deployment must define how the epoch
survives rollback or is advanced outside the restored state. If that evidence
is unavailable, retain inhibition.

Retain journal entries and deduplication tombstones for at least the maximum
admitted-request lifetime, retry/recovery window and supported restore horizon.
Requests older than the supported horizon are refused pending explicit
reconciliation; expiry of a receipt never means that its effect did not occur.
State the concrete periods in installation qualification, not an implicit
forever guarantee in application code.

Rollback is a separate authorized operation. It names the original receipt,
expects the current pointer to still equal that operation's published revision,
and conditionally restores the prior pointer with its own receipt. A newer
publication causes a conflict requiring a new decision. Reversal restores the
previous content pointer while advancing the target revision; it never reuses an
old revision counter. This prevents an old precondition from becoming valid again
after content returns to its earlier value. Original publication
and acceptance history remains intact. Retain the prior candidate for the
declared reversal window; otherwise reversal is unavailable, not best effort.

## Output and observation boundaries

Private source rows, artifacts, raw target responses and credentials stay on
the resource side. Even identifiers and digests can reveal information. The
owner installs and checks the exact output projection and destination before
release, and rechecks current reader authority on later retrieval. Cloud
receives only permitted references, coarse states and released evidence. Use
the existing output contract rather than treating receipt possession as access.

Current resource-owner v2 permits C0 to C0, C1 to C1 and C2 to C2 only;
declassification is unsupported. Hashing, redacting or coarsening a private
receipt does not make it C0. Keep the first fixture C0. Any later cross-class
Cloud projection requires a separate qualified output contract; otherwise use
an authorized same-class destination.

Scope the exclusivity claim to this operation class and the identities assessed
in the installed target. Prove denied direct writes from each worker/tool role.
An observed change without a matching gate receipt is an **unattributed target
change**, not proof that an agent bypassed the gate. Attribute administrative
changes and other controllers, and report observation gaps, before calculating
coverage. No closed observation scope means no complete-coverage claim.

## Delivery and falsification gates

These are work-package labels, not created Sprintctl item IDs. Shape owner work
against the exact reviewed plan revision before implementation.

| Package | Deliverable | Exit evidence |
| --- | --- | --- |
| Intent binding | Versioned closed operation contract and attempt-to-business-operation mapping, without changing existing IDs | Compatibility tests; changed-payload conflict; different request ID for identical intent refused or reconciled. |
| Target publication | Installed lab schema/procedure, exact verifier and constrained roles | Atomic pointer/journal failure tests; expected-revision and target-epoch enforcement; direct worker writes denied. |
| Reconciliation | Read-only status adapter, durable evidence references and restart recovery | Commit-then-drop-response exercise; successor derives one committed publication from target evidence. |
| Work integration | Existing Work/Evidence composition and current customer acceptance policy | Successful effect with failed verification remains unaccepted; stale performer cannot write acceptance. |
| Installed qualification | Versioned installation record and independent replay of the bounded workflow | AgentOps evidence pack, measured operator effort and retained rollback/recovery instructions. |

The required adversarial cases are:

| Case | Falsifying result |
| --- | --- |
| Candidate bytes or verifier changed after authorization | Changed content publishes or is accepted under the original basis. |
| Target pointer changes before publication | The operation overwrites the newer revision instead of refusing its stale precondition. |
| Same intent arrives from two attempts or under different request IDs | More than one publication effect is recorded for the intent. |
| Old attempt writes after the target acknowledges a new epoch | The stale effect commits. |
| Effect commits, then the process/connection dies before acknowledgment | Recovery republishes or reports absence without establishing the target outcome. |
| Owner, gateway or target read is unavailable | The system treats UNKNOWN as failure-safe-to-retry, completion or acceptance. |
| Worker has a direct write route | The assessed operation can bypass target checks through that route. |
| Verification fails after a real effect | The effect disappears from history or Work is accepted regardless. |
| Restore predates a committed operation or authority change | Recovery reuses the old epoch, loses the effect, or retries before reconciliation. |
| Receipt is outside its retention/restore horizon | Missing historical evidence is treated as proof that the operation never happened. |
| Target alias or configuration is changed after dispatch | A retained follow-up reaches a replacement incarnation or treats its missing journal row as absence at the original target. |
| Another operation publishes before reversal | Reversal overwrites its revision. |
| External change or incomplete observation has no gate receipt | It is counted as a proven bypass or omitted from the declared coverage denominator. |
| Output authority is revoked or projection changed | Retained private bytes or a previously allowed result are disclosed without a fresh valid decision. |

Run these against the actual installed procedure, roles and artifact digests,
with an independent observer for the commit/lost-response boundary. Unit tests
and synthetic native responses remain supporting evidence. Record the failure
injection point, target query and retained receipt for each exercised history.

The initial rollout is an isolated, disposable target with synthetic data and
an explicit operation allowlist. Disable the new operation to roll back the
adapter; retain its journal and candidate artifacts for recovery. Revert schema
only through a reviewed migration after outstanding intents are reconciled.
Do not use a live Cloud rollout, credential rotation, production failover or a
data restore as the first proof.

Expand only after useful work is completed with acceptable total operator
effort, a second operator can recover the recorded failure, and a second
operation can reuse the contracts without introducing another execution
engine. Native harnesses keep planning/execution; existing workflow systems
keep their retries; resource adapters establish the domain-specific effect.
