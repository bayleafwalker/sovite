# Local private task receipts for Vuoro

The Python 0.1.16 owner release adds an optional owner catalog integration.
Container/Helm deployment examples continue to select their published 0.1.15
images; publishing this wheel does not upgrade any running installation.

`register_sovite_catalog` exposes only `sovite.task.request-v1` and
`sovite.task.status-v1`. It requires an explicit loopback installation binding,
a distinct caller credential for each authenticated workspace/repository/principal
and an owner-supplied Work authorization callback. Both operations recheck the
native Work owner before and after upstream access. The callback must authorize
the exact current work attempt; it must never treat an HMAC receipt binding as
permission. The task correlation key must survive a composition restart.

Task submission uses an idempotency key scoped to that authenticated tuple,
work and run. Status checks require an intact work/run/grant/receipt reference.
The upstream caller remains subject to Sovite's live grant and receipt access
checks. Exactly receipt, coarse status and an integrity binding cross the
catalog response; private artifacts, model output, setup/activation and operator
API methods are absent. Conflicting retries remain owner refusals. A response
lost after dispatch can leave an unknown outcome; reconcile the same key/receipt
rather than creating another task attempt. Revocation does not undo inference
already admitted, nor does a post-dispatch Work refusal undo that side effect.

Model completion now fails closed on explicit `length` or `content_filter`
finish reasons before tool dispatch or artifact publication. Successful results
retain finish metadata. Legacy adapters without finish metadata remain possible;
this workflow's verifier additionally requires `finish_reason=stop`.

The old `qwen3:4b` cache was Thinking-2507. The local final-answer assessment
explicitly selects and digest-checks `qwen3:4b-instruct-2507-q4_K_M` instead.
No alias is silently remapped. The known thinking-only tuple is refused before
source content is sent. Model cards are supporting context; installed `/api/show`
metadata and observed live completion are the execution evidence.

`local_evaluation` is a development fixture for C0 synthetic input, with explicit
SQLite migration before runtime startup, separate API/worker processes and no
signing key in their environment. It permits local agent-led model/agent draft
creation, operator-only activation and setup-principal revocation. Its explicit
one-day development assertion is not production or hosted qualification.
The supplied instruction text is a consumer contract, not an owner truth policy.

Vuoro's optional `private-workflow` consumer records receipt links in existing
Work/Evidence, interrupts the producer, recovers the durable link in an independent
verifier, and reads artifacts through separate authorized access. A strict bounded
handover profile verifies the fixture, after which the accepting owner rereads
source/output and countersigns its own protected receipt. Sprintctl requires
that accepting identity binding. Final Work acceptance is a separate decision.
No raw private bytes are uploaded into coordination evidence.

Boundaries: local bearer identities; C0 only; no provider federation, hosted
agent-led setup, production secrets, protected filesystem writes, distributed
isolation or general semantic competence. Completion of this workflow does not
close protected-operation qualification gates.
