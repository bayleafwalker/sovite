# Caller discovery and attempt lineage

This extends PR #4's intended class-aware/scoped-reader contract. Reference grants
preserve receipt-only compatibility. The subscription routes in that work still
have no live-account qualification; discovery does not add it.

Preferences, route selection and spending decisions stay with the coordinator.
Using a subscription allowance this week does not require a bundle generation
change or imply a spending cap. Recording an expensive fallback explains it;
preventing a bill requires a separately stated constraint in the spending owner.

## Discovery

MCP `list_candidates` accepts only optional `input_class`. Omission selects the
highest installed class, just as submission does. It exposes only grants naming
the authenticated caller. Reader scope alone does not authorize discovery of a
producer. Instructions, endpoints, credential selectors, other callers/readers
and private results are omitted.

The bundle can install `discovery_class: C1` for operational metadata. The label
must resolve. Omission defaults to the highest installed class without changing
legacy bundle hashes. Each candidate's grant clearance must meet that label;
otherwise only its owned grant ID, return contract and clearance marker appear.

Detailed responses include generation/time and separate declared IDs, adapter,
harness kind/capabilities, inference boundary, input ceiling/output class, return
contract, workflow-only status and configured concurrency from observed current
grant-policy/installation-qualification facts. Workflow-only routes say
`bound_workflow_required`; the author must still bind the real workflow.
Shared gateway runtime activity is not probed or disclosed. Capacity is
`not_probed`. Qualification is installation/profile qualification, not evidence
of an unperformed live subscription-account test. A listing is not a reservation.

An explicit `interactive-authentication` capability advertises support for that
path. `human_presence_verified` remains false: the protected action's owner must
require and verify human presence. Subscription login configuration alone does
not infer this capability or grant effect authority.

`begin_task.expected_generation` can reject a changed discovery snapshot. The
actual selected recipe is always retained. Optional `selection_reason` is private
job data under its class/placement, excluded from model input, caller projections,
discovery and audit details. Missing rationale is recorded as `not_supplied`.

## Attempt lineage and retry

`previous_attempt` names an earlier receipt owned by the same caller. It records
provenance without marking that result superseded, reading its artifact or
declassifying it. A completed earlier job can be lineage after its grant/generation
is removed; this grants no current content access or transfer authority.

In-flight prior work is refused. A started unsuccessful non-replay-safe execution
remains uncertain after cancellation, timeout, revocation or activation, regardless
of its final error label. At first linkage, started model-gateway work needs retained
ledger evidence that every request stopped; missing/expired or unconfirmed evidence
refuses a new link. The owner captures positive stop evidence in the lineage record,
so normal ledger retention does not invalidate an admitted successor. Later contrary
evidence and identity changes still refuse it at dispatch, renewal and release. A changed
binding does not resolve the previous effect. The server does not infer logical
equivalence between unrelated new keys with no lineage.

Use the same caller-scoped idempotency key for one intended invocation. Exact replay
recovers its same-role receipt without re-execution or re-admission after revocation;
current release checks still apply. Changed binding/rationale/lineage conflicts,
with the owned receipt returned on MCP or workflow HTTP `idempotency_conflict`. Distinct unfinished
and uncertain-effect errors let the caller reconcile through its owner.

Artifact reuse remains the separate `source_release`/`transfer_id` operation:
current source authority/access, exact artifact identity, actual approved output
class, destination eligibility and required release approval are checked there.
Being the same caller or supplying lineage is insufficient. A previous receipt
cannot bypass a workflow-only route or an approved-output requirement.
