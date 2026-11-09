---
doc_id: sovite-decision-observation-feedback-2026-11-09
title: Execution observations for decision and lane feedback
purpose: proposal
lifecycle: ratified
effective: 2026-11-09
subjects: [execution, evidence, capacity]
related: [vuoro-decision-records-and-lane-feedback-2026-11-09]
---

# Execution observations for decision and lane feedback

**Status:** Accepted planning scope, 9 November 2026. Interfaces, capture profiles
and integration remain proposed and unqualified. This refines SV-PW2/5; it
changes no wire schema, configured quota, admission rule or deployed capability.
The [Vuoro decision-record plan](https://github.com/bayleafwalker/vuoro/blob/main/docs/plans/2026-11-09-decision-records-and-lane-feedback.md)
owns the shared record and concurrent observation/allocation loops. Cheap agents
and bounded Jev consumers are primary; a UI is an optional projection.

## Ownership

[Lanectl](https://github.com/bayleafwalker/lanectl) already implements capacity,
window, probe, burn and learning paths in source; its example quotas are not live
account evidence. The lane owner selects allocation and routes under existing
policy. Sovite supplies execution observations and applies the selected binding
through current admission. It gains no scheduler, provider router or acceptance
authority. Work acceptance remains the Work owner's decision.

[Caller discovery](../CALLER-DISCOVERY.md) remains unchanged: declared concurrency
is not available capacity, discovery is not a reservation, and `not_probed` stays
explicit. A quota allocation cannot expand a grant, data boundary, account
entitlement or paid fallback permission.

## Observation increment

Bind an upstream decision reference to the existing job, attempt and receipt.
Retain admission/refusal and actual application separately from the proposal,
including expired or superseded advice. Record requested, admitted and observed
model/harness/environment together; unsupported effective settings remain unknown.
An OpenInference `DECISION` span describes a decision-model call, not this durable
decision record or an owner acceptance.

Expose scoped usage/capacity observations with their source, units, account-pool
and window references, refresh semantics, observation time, applicable time and
freshness. Distinguish provider observations, estimates and configured limits.
Preserve shared-pool and overlapping-window relationships, in-flight usage and
unknown external account consumption. Changed headroom does not prove an old
attempt stopped or justify duplicate dispatch. Selection probabilities belong to
the actual selecting policy; model confidence cannot substitute for them.

Derive expected attempts from owner records and reconcile missing observations.
Preserve resource/effect references and existing output-class checks. Private
`selection_reason` and raw provider records do not become public discovery data.
New fields require the owning versioned contract; do not widen a closed wire
through arbitrary metadata.

## Proportional capture

Use compact decision/basis references for local reversible work. Add bounded
usage and outcome joins for allocation and escalation; retain complete required
authority, operation, receipt and verification facts for protected effects.
Never sample away those minimum protected facts. Avoid copying full snapshots
or calling a model merely to record a cheap decision.

Apply the shared plan's proposed metadata, capture-latency and optional-inference
budgets per capture depth, then measure bytes, processing, storage and any adviser
calls. Exceeding
the budget disables optional detail or advice with an explicit coverage gap;
required effect evidence follows its existing owner failure policy.

## Verification and rollback

Fault tests cover stale advice, changed authority, missing observations, shared
quota pools, uncertain predecessors and wrong output scope. Correctness failures
block the affected path. Efficiency evidence reports quota consumed, work landed
and total overhead over declared windows, directionally unless comparison is
supported. Neither loop waits for the publication showcase.

Rollback disables the optional observation/advice integration, preserves durable
owner records and in-flight accounting, and returns selection to the previous
lane policy. Reconcile outstanding effects through their existing owners.
