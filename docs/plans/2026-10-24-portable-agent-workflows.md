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
