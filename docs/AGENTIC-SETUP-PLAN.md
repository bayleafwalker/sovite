# Sovite agent-led setup: first implementation slice

Direction: 20 October 2026. Refines the accepted Sovite-over-Cloud S2/S3 work;
local source implementation does not qualify hosted use or authorize deployment.
The cross-repository sequence is in Vuoro's
`docs/plans/2026-10-20-agentic-private-workflow.md`.

## S-AI-01: bounded setup-agent principal and draft

Source implementation adds an opt-in API, disabled by default. An operator mints an installation-bound,
short-lived setup credential with a durable hashed credential record, stable opaque
principal ID, operator issuer recorded in audit, expiry, revocation and explicit configuration envelope.
No raw token is stored or written to audit. Agent package names never authenticate.

Setup agents can create and inspect their own model/agent proposals. They cannot
read other authors' drafts, mint principals, activate bundles, issue qualification,
submit operator jobs, read artifacts, inhibit grants or become ordinary task callers.

The proposal is a full validated revision derived from a compare-and-set active base.
Only additive models and agents, plus explicitly allowed existing-grant agent binding
changes, are accepted. Endpoints, destinations, adapters, harnesses and tools are
bounded by the issuing operator. The first API selects one exact owner-approved model
template, adds one agent with no tools, and binds one allowed existing grant. Existing authority, classifications, credentials,
placement, grant callers and runtime settings cannot be edited by this principal.

Owner activation must use the existing bundle activation semantics and atomically
recheck the author principal's current validity, base generation and draft state.
A revoked author or changed base refuses activation. Activation of the same exact
proposal is idempotent only while it remains the current revision; it does not
silently reactivate a revision after a later change. Existing qualification rules
and generation invalidation remain mandatory.

Add migration 4 for typed principal/draft tables. Runtime `migrate=never` does no DDL;
old schemas refuse startup. Preserve SQLite and isolated PostgreSQL behavior.

## Test and first real workflow

Use separate setup and operator HTTP clients, persistent SQLite, existing policy
validation, actual worker lifecycle and private storage. Exercise restart, expiry,
revocation, isolation between two principals/installation IDs, forbidden categories,
endpoint/tool/step widening, stale base and activation replay. Fail the negative
cases before accepting the happy path.

A deterministic adapter integration proves the new API/state boundaries only. A real
WF-01 run additionally needs an observed real model endpoint, exact model/runtime,
a supported worker, trusted qualification for that generation and Vuoro owner-side
verification/acceptance. A missing endpoint is reported as unobserved/unavailable;
there is no synthetic fallback and no production security claim.

Next: S-AI-02 supported setup CLI/HTTP consumer, then released owner adapter and
Vuoro work/evidence correlation. Hosted assertions, role-separated qualification,
external worker/helper enrollment and two-tenant proof retain existing S2–S6/CL-K06
dependencies. Onward delegation and unattended activation are not granted here.


## Source evidence and next gate

`tests/test_setup_api.py` exercises the complete new setup-to-worker path, including
blocked dispatch and retry. It uses `synthetic-assessment-v1` deliberately and makes
no real-inference or Vuoro acceptance claim. The operator procedure and complete
request shapes are in [operations](OPERATIONS.md#bounded-agent-led-setup-opt-in-source-capability).
The initial probe on 20 October found no listener at `127.0.0.1:11434`. A later
inspection found the pre-provisioned model cache and Ollama image. An owned temporary
server on loopback port 11531 then ran the real assessment below; it was stopped
afterward. The historic REAL-INTEGRATION record was not reused as fresh qualification.


## Fresh real inference assessment

[AGENTIC-REAL-ASSESSMENT.json](AGENTIC-REAL-ASSESSMENT.json) records the actual local
setup principal → proposed model/agent → operator activation → unqualified dispatch
refusal → explicit development authorization → caller retry → separate worker →
private artifact → fresh-client checksum readback. Qwen3 4B through Ollama 0.40.6
completed this assessment in 95.721 seconds, producing a 3,870-byte artifact; the
caller received only receipt/status and was denied the artifact endpoint. No
synthetic fallback ran. Source M1–M3 references were present; this is not semantic
verification or an accepted Vuoro work item.

Reproduction uses `scripts/qualify_agentic_setup.py` with an explicit loopback model
endpoint, expected model digest, the Vuoro-owned C0 fixture, a new summary output and
a private artifact path. It creates disposable API/worker state and local signing
keys, removes that state and its processes at exit, and retains the requested exact
artifact privately. The caller and setup credential cannot sign qualification.
The development operator assertion authorizes only this assessment and is explicitly
not production deployment evidence. Serving used a cached CPU image with 4 CPUs and
6 GiB memory limits; no production network isolation is asserted.

Attempt denominator: four assessment-script invocations. The first refused a
non-canonical model digest before inference; the second correctly blocked dispatch
but exposed a wrong test expectation that the bounded caller error would disclose
qualification details. The corrected third invocation completed real inference but content review rejected
its planning narrative and incomplete handover. A fourth invocation used only the
source snippets, a shorter explicit final-answer contract and a larger output budget;
it completed one worker attempt in 121.167 seconds but again failed content review.
The [retry record](AGENTIC-REAL-RETRY-ASSESSMENT.json) preserves its separate hash.
Thus two invocations failed before inference and two produced real outputs that were
not acceptable task outcomes. The first two are not model-quality failures.

The observed Qwen/runtime/instruction tuple is not qualified for this handover task.
A supported model/prompt-template contract and fresh task-competence evidence are
required before proceeding to accepted WF-01. Do not repair output by substituting
scripted text, silently switching models, or treating artifact availability as success.

Remaining gates: released owner adapter, durable work/run/receipt correlation,
independent semantic verification/acceptance, distinct-session interrupted recovery,
and hosted qualification. This source assessment closes none of those gates.
