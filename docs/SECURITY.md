# Security model and deployment evidence

Sovite is assessed as a complete installation: serving, harnesses, workers, control API, private state, caller receipts, operator console, qualification and recovery. This document describes the implemented trust boundary. An installation becomes suitable for restricted production data through its concrete placement, identities, network controls and evidence; selecting `mode: enforced` alone does not establish those properties.

## 1. Responsibilities and trusted components

Each control owns a different invariant. A signature cannot replace network isolation, and a network policy cannot decide whether a caller still has authority.

| Component | Implemented responsibility | Trust required |
|---|---|---|
| Bundle compiler | Resolves references, checks declared information classes and destination clearances, rejects unsupported contracts | Accurate labels, endpoint ownership and package declarations |
| Control API and worker | Authenticate callers, evaluate current grants, hold durable execution state, authorize release | Their code, host, credentials and database access are trusted |
| Database and artifact store | Retain private tasks, results, authority state and audit records | Approved storage placement, access controls, backup and encryption configuration |
| Native harness service | Runs a fixed agent and generation; keeps its own execution ledger | The service and its pod remain trusted within that private boundary |
| Kubernetes/Cilium and gateway | Enforce network reachability, DNS allowances, service identity and HTTPS entry | Correct reconciliation and administration of the actual cluster |
| Qualification issuer | Signs scoped evidence for the generation and runtime binding | Signing-key custody and truthful evidence collection |

The control plane and workers have broad private-state access. The shipped identity model separates operators from named callers; it does not provide independently isolated tenants, enterprise SSO, delegated per-object operator roles or an integrated rate-limiting service. [Implementation: API and persistence](../sovite/api.py), [Helm runtime environment](../deploy/helm/sovite/templates/_helpers.tpl).

## 2. Information flow and current authority

The compiler uses an ordered set of declared classes. An agent's context includes its input and every permitted tool's result class. Its output cannot lower that class. Inference, execution and private-state destinations must accept the resulting context. A tool is also a **destination for arguments**: its input allowance must accept that agent's context, as well as its own destination accepting arguments and results. The implementation does not infer sensitivity from arbitrary text or verify that a tool's owner labeled its data correctly. [Compiler](../sovite/policy.py).

Admission checks the caller, enabled grant, input class, runtime inhibit and qualification. Jobs retain their resolved recipe and generation. Dispatch, lease renewal, every HTTP request mediated by `AdapterRunner.request_json`, and result finalization recheck current authority. Changing the active generation blocks older queued work and requests cancellation of running work; a worker cannot silently move an existing task to another model or route. Upstream failure has no automatic model fallback. [Authorization](../sovite/service.py), [worker lifecycle](../sovite/worker.py), [adapters](../sovite/adapters.py).

An inhibit denies further dispatch and public release. Cancellation of an already running operation is best effort: the worker observes it during renewal, cancels local execution and can call a declared remote cancellation endpoint. Revocation cannot retract data already transmitted or undo an external effect. Native Codex's internal provider requests do not individually call the worker's authorization function; the separate service, periodic renewal, cancellation and network boundary govern that interval.

## 3. Caller receipts and private operator access

The caller MCP interface supports initialization, ping, tool discovery and two tools: `begin_task` and `task_status`. It does not implement private task retrieval, MCP Tasks result retrieval, resources, sampling or arbitrary notifications. Unsupported methods are rejected. The opaque `rcpt_…` identifier is generated independently of the private job ID and is bound to the authenticated caller; possession alone grants nothing. Model output cannot choose the public identifier, error text or projection. [Caller surface](../sovite/api.py).

Public status is limited to `pending`, `working`, `available` and `unavailable`, and is reevaluated against current authority. No result URL, private job ID or tool output is released. These finite statuses still carry information, and timing, workload contention and covert channels are not closed by this design. A receipt test establishes the tested response boundary, not complete noninterference. [Receipt authorization](../sovite/service.py).

Operators deliberately have a separate private read path. An authenticated operator can inspect retained artifacts even after a grant is inhibited; inhibition governs execution and caller release, not operator investigation. The console fetches artifact bodies only after an explicit open action. Sessions use server-side records, hashed cookie tokens, HttpOnly/SameSite cookies and exact-origin checks for cookie-authenticated mutations. Deploy remote access through the approved HTTPS gateway with secure cookies enabled. [API contract](API_CONTRACT.md).

## 4. Execution and network isolation

The bounded tool adapter selects configured URLs, operation allowlists and tool identities. Kubernetes reads use fixed resource paths and namespace; model arguments cannot choose a different URL, verb, namespace or credential. HTTP redirects and ambient proxy configuration are disabled for mediated requests. An HTTP tool's `read_only` declaration and an external harness's `idempotent` declaration are operational promises to verify, not properties derived from their names. [Adapters](../sovite/adapters.py).

Inline native execution is a development facility. Enforced inline execution is rejected. The separate native service receives a fixed agent/generation and service token, with its own persistent ledger; the supplied manifests do not give it platform database, object-store or operator credentials. Codex runs with a temporary home, a reduced environment and its read-only sandbox. This is service-level isolation, not a separate kernel or filesystem for every caller: tasks in that service share its trust boundary and retained state. [Native service](../sovite/harness_api.py), [native deployment](../deploy/helm/sovite/templates/native-harness.yaml).

The Helm chart declares default-deny ingress and egress, then specific control, state, inference, native and tool connections. Enforced chart rendering requires Cilium DNS query restrictions, secure ingress settings and image digests. DNS permissions and network reachability are separate rules. The optional diagnostic service account has namespaced read permissions rather than cluster-admin. These manifests require a functioning enforcing CNI and site-specific gateway selectors, API addresses, tool peers and DNS names. External tool services need their **own egress controls**; Sovite's worker policy does not constrain a separate tool server. The same holds for a model host outside the cluster: an exact-CIDR model peer grants reachability to that address only, and the deployment contract requires HTTPS to it, optionally pinned to one certificate (`tls_trust`), because prompts and the provider credential cross networks the cluster does not control. The host's own firewall, its authentication of the provided credential, its prompt retention and the model it actually serves are outside the chart. Generic serving engines have no supported identity protocol, so such models run unpinned and are rejected in enforced mode. [Network policies](../deploy/helm/sovite/templates/network.yaml), [rendering constraints](../deploy/helm/sovite/templates/validation.yaml), [worker identity](../deploy/helm/sovite/templates/worker.yaml).

## 5. What qualification establishes

Ed25519 qualification binds the declared bundle generation, profile, issuer, validity window, outcome, evidence payload and nonsecret runtime fingerprint. Verification checks a configured trusted public key, signature, current binding and expiry. The fingerprint covers database identity, artifact location, origin, cookie/qualification settings and the optional `deployment_binding` value. It deliberately excludes secret values. It is not hardware attestation or proof that the advertised network rules, image or model actually executed. [Qualification implementation](../sovite/qualification.py).

The Kubernetes installer computes that deployment binding from the reviewed rendered resources, bundle generation, merged public values, chart-file hashes, public trust and release/context/namespace. It normalizes the binding's self-reference, excludes Secret values, and injects the same value into control and worker. Before apply it cross-checks package and service bindings and the configured API addresses against observed EndpointSlices. This binds desired manifests; it does not attest live CNI enforcement or detect privileged cluster changes. [Installer](../scripts/install_kubernetes.py), [deployment contract](../scripts/deployment_contract.py).

The model adapter checks an approved Ollama identity endpoint's reported digest when a model is pinned. This is an endpoint assertion, not cryptographic attestation of every inference. Prevent mutable model replacement during execution and collect evidence for the serving process and loaded weights. Generic provider APIs without the supported identity mechanism are rejected in enforced mode. [Identity verification](../sovite/adapters.py), [bundle constraints](../sovite/policy.py).

Run administrative commands with the installation's actual runtime settings and active bundle. Initialize the signing key once in the signing environment; runtime services need only public trust.

```bash
sovite validate config/platform.kubernetes.yaml
sovite runtime-binding
sovite keys init
sovite qualify --evidence evidence/deployment.json --profile kubernetes --valid-days 30 --issuer deployment-operator
sovite audit verify
```

`qualify` signs supplied evidence; it does not run the site's deployment tests. Evidence must contain the exact `generation`, matching `profile`, nonempty `scope`, and a nonempty `checks` array with `name` and `status`. Status is `passed`, `failed` or `not_run`; any non-passing check makes the qualification fail. Enforced admission additionally requires `evidence_kind: deployment`, the exact `runtime_binding`, and these **exact passing check names**:

| Required check | Evidence should establish |
|---|---|
| `runtime-identity` | Actual images, loaded model, harness and placement match the qualified binding |
| `network-isolation` | Required controls are active, allowed paths work, and forbidden paths are blocked, including tool egress and DNS |
| `receipt-boundary` | Caller success, failure and revocation paths do not expose private payloads |
| `recovery` | Restart, lease recovery, uncertain effects and restored authority behave as intended |
| `task-competence` | The selected model/harness/tools perform the intended tasks within an explicit acceptance scope |

The built-in `sovite assess --output evidence/synthetic.json` exercises the whole synthetic installation workflow. It rejects real-model profiles and does not produce production deployment evidence. Qualification is required in enforced bundles and cannot be disabled there. [CLI](../sovite/cli.py).

## 6. Persistence, recovery and key changes

Private artifact persistence precedes the database transaction that records its reference, the trusted projection and release audit event. Finalization rechecks the lease and authority. Filesystem writes use a temporary file, fsync and atomic replacement; reads verify the stored digest. S3 and the database do not share a transaction: a crash or expired lease can leave an unreferenced private object. A recorded release does not prove network delivery to a caller. [Artifact storage](../sovite/storage.py), [finalization](../sovite/worker.py).

Expired leases are replayed only under the implemented read-only/idempotency rules. Potentially effectful uncertain work is blocked or failed instead of blindly repeated. The native service keeps a durable request ledger and marks interrupted running entries uncertain after restart. Preserve that ledger with platform state. Audit hash linkage detects inconsistency relative to its stored head; a database administrator can rewrite the entire history and head. Independent checkpoints are needed to detect that replacement. [Recovery](../sovite/worker.py), [audit chain](../sovite/db.py).

Rotate runtime secrets separately from qualification: changing a secret value does not change the nonsecret fingerprint, while changing placement or public trust can invalidate evidence. Restart services that load credentials from their environment. Existing operator sessions currently survive token rotation until logout, expiry or explicit administrative removal of their session records. Removing a signing key from trusted public keys invalidates its qualifications without needing to rewrite historical records.

After restore, retain current desired configuration and trust, apply `sovite recovery-lock --reason "Restored state requires review"`, verify the audit chain and review outstanding effects before clearing inhibits. Recovered session records and private backups also belong to the private boundary. See [operating and recovery procedures](OPERATIONS.md) for the provisioned profiles and their concrete storage duties.
