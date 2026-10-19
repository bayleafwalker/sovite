# Sovite Platform 0.1.0 — release readiness

19 October 2026

## Outcome and assessment boundary

The complete reference platform is implemented. It includes the operator console, control API, durable work queue, private result storage, caller-bound receipts, package configuration, policy validation, qualification, native and bounded agent execution, deployment tooling and recovery procedures. The installed system is the unit of assessment; individual tests support the assessment of its useful workflows and operating behavior.

The local application has run with separate API, worker, native harness and model-serving processes. Both direct private inference and an actual native Codex file-reading task completed through the full task/result/receipt path. The source also contains the full Compose and Kubernetes distributions. Their configuration has been rendered and checked, but this workspace has no Docker daemon, Kubernetes context or target credentials with which to claim an actual container or cluster deployment.

This is a complete implementation for whole-platform assessment. Production qualification remains specific to the target's actual images, model, network, storage, identities and workload acceptance criteria.

## What is implemented

| Area | Working release capability |
| --- | --- |
| Package configuration | Strict YAML/JSON schema; independent model, harness, agent, tool, compute, destination and grant identities; serving protocol and endpoint bindings; immutable whole-bundle generation |
| Control and execution | Separate API and workers; durable job state, compute-pool routing, idempotency, leases, attempts, cancellation, retry limits and uncertainty handling |
| Private data flow | Declared context-class calculation across input, tools, inference, compute and state; independent public receipts; private artifacts with verified content hashes |
| Inference and harnesses | Ollama and OpenAI-compatible adapters; bounded tool agent; maintained native Codex CLI in a separate HTTP service; external agent HTTP contract |
| Production tool interface | Fixed-path, namespace-scoped Kubernetes reads and configured HTTP operations; argument and result classifications; no arbitrary Kubernetes command endpoint |
| Operator application | Eight connected views; actual configuration and operating state; task submission/cancellation, deliberate artifact access, grant inhibit/resume, qualifications, revision history and audit |
| Qualification | Ed25519 signatures over generation, profile, issuer, expiry, evidence and nonsecret runtime/deployment binding; trusted public keys deployed separately from the signer |
| Distribution | Local Python installation; complete Compose services; Helm chart and GitOps example; model staging, image locking, configuration review, scoped RBAC and network policies |
| Recovery | Durable worker recovery, uncertain-effect handling, state export, coordinated Compose backup/restore, recovery inhibits and audit verification |

Serving engines, model weights, containers and cluster reconciliation retain their normal upstream tooling. Sovite supplies the bindings, lifecycle and operating contract around the complete installation. It does not implement another tensor server, model registry, cluster scheduler or replacement native Codex loop.

## Application and operator verification

The final backend and policy suite passed **40 tests**: 21 backend tests and 19 policy tests. These cover the task lifecycle, caller/operator separation, origin checks, idempotency, current authority at mediated boundaries, generation replacement, lease fencing and recovery, uncertain effects, artifact/audit integrity, qualification signature and runtime-binding checks, native service request identity and cancellation, and empty-answer rejection. The single emitted warning concerns Starlette's deprecated use of httpx in its test client.

The browser pass recorded **40 passing checks** against the real-model installation. It covered all eight views, component details, filtering/search, qualification and audit inspection, keyboard dialogs, session login/logout and desktop/mobile layouts. It observed no JavaScript or CSP errors, no external browser requests, and no automatic private-artifact fetches. A populated mobile table exposed an overflow that the earlier empty-state view had not exposed; that layout defect was corrected and the affected check passed.

The earlier mutating browser workflow also exercised task submission, private artifact access, inhibit/resume and logout on synthetic work. The final real-model browser pass was deliberately read-only apart from its own session.

Detailed records are included under `docs/`: `UI-VALIDATION.json`, `REAL-INTEGRATION.json`, `DEPLOYMENT-VALIDATION.json` and `STATE-INTEGRATION.json`. These report their own scopes; a pass in one record is not silently promoted into a different claim.

## Actual local-model and native-harness execution

The real model run used Ollama **0.40.6**, Codex CLI **0.163.0**, and the actual `qwen3:4b` weights. The serving process reported an 8,192-token context and CPU execution with no VRAM allocation. Both declared model bindings were checked against the observed Ollama digest:

```text
sha256:359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7
```

The same weights were used through two independently configured interfaces: Ollama chat for the bounded agent, and the Responses API for the native Codex service.

| Actual path | Observed result |
| --- | --- |
| MCP caller → durable job → worker → Ollama → private artifact → caller receipt | Completed in **43.418 seconds**, one attempt; nonempty 2,142-character private answer |
| MCP caller → durable job → worker → native service → actual Codex CLI → Ollama Responses | Completed in **511.482 seconds**, one attempt; private final answer retained |
| Native workspace tools and continuation | Codex executed the real `exec_command` tool twice to read a numbered source fixture; both commands exited successfully; their results entered a subsequent model request |
| Native task result | Correctly reported the fixture's four values and source lines, and computed a **96-second** budget |
| Earlier native task cancellation | Reached terminal `cancelled` state and stopped the native subprocess in **9.064 seconds** after the operator request |
| Replayed caller submission | Returned the same receipt without creating another job |
| Public projection | Exactly `receipt` and `status`; no fixture value, private job ID, result body or artifact URL |
| Caller private-API attempts | Job listing and artifact retrieval denied with HTTP 403; unsupported task retrieval rejected; foreign Origin denied |

This used an authenticated HTTP JSON-RPC test client. An OpenAI cloud-account caller was not exercised. The shipped Codex MCP configuration supplies the corresponding caller integration, while the native CLI itself was actually executed on the private side.

### What the real run changed

The first bounded inference request consumed its output budget while thinking and returned empty final content. The initial adapter incorrectly accepted that content as a completed result. The corrected adapter rejects empty or whitespace-only model and native final answers before publication, and the focused regression passes.

The model package now explicitly carries Ollama `think` and native `reasoning_effort` settings. Ollama `think` is forwarded at the protocol's top level. Native reasoning effort is forwarded through Codex configuration. The corrected bounded task used `think: false`; the native task used explicit reasoning effort `none`.

### What the successful native task does not establish

The native task took about 8½ minutes on this CPU runtime, read the same file twice, and retained surplus reasoning text and a stray thinking delimiter in its final answer. Those defects are visible in the assessment; the platform does not silently clean the result and call the model generally competent.

Ollama's observed native prompts were 3,955 and 5,945 tokens under the configured 8,192-token context. No context truncation was observed. The two native Responses requests took approximately 330.491 and 175.106 seconds. The run demonstrates actual native process/tool/continuation integration and correct performance on this fixture. It does not establish acceptable latency, answer quality or reliability across realistic coding work.

The bounded model answer also reached its configured 512-token output budget in the assessment. Its nonempty result and private flow were verified; answer completeness was not benchmarked. A different model, GPU, context size, harness configuration or workload requires its own observations and qualification.

## Deployment and durable-state verification

The local installer has been exercised through initialization, separate API/worker start, authenticated task submission, caller receipt lookup, stop and restart. Completed jobs, private artifacts, public projection, current qualification and audit history survived the restart. The task-owned installer instance was stopped cleanly after verification.

The distribution review used Helm **4.3.1** and Docker Compose **5.6.0**. Four Compose configurations parsed successfully: synthetic, native, native with GPU, and native with an approved input directory. The complete Kubernetes reference and the enforced structural fixture each produced **30 reviewable resources**. **22 invalid configurations were rejected**, including mismatched package/compute/service bindings, insecure settings, missing or broad API addresses, mutable enforced images, and invalid workspace placement.

The Kubernetes installer includes the actual public bundle and qualification trust in its review, keeps runtime secret values out of rendered output, and computes a stable deployment fingerprint over the reviewed resources and inputs. The API and worker receive the same binding. A network-policy edit changes the binding. The enforced rendering used explicit structural fixtures to exercise its constraints; those fixtures are not claimed as real deployed image or cluster identities.

The separate durable-state installation used actual PostgreSQL **17.11** and Garage **2.4.1**, with independent API and worker processes and the unchanged synthetic execution adapter. Its **12 checks passed**. PostgreSQL had `fsync`, `full_page_writes` and `synchronous_commit` enabled. Both state services ran as an unprivileged account on the isolated loopback fixture.

| Actual state workflow | Observed result |
| --- | --- |
| Private artifact publication | Database digest, private API bytes and S3 object bytes agreed; caller artifact access and anonymous S3 access returned HTTP 403 |
| API and worker restart | Completed job, receipt, artifact, qualification and idempotency survived without replay |
| Abrupt worker failure | A replacement worker reclaimed an expired PostgreSQL lease, safely retried the synthetic task on attempt two and released exactly one result |
| Concurrent dispatch | Two workers completed four queued tasks, one attempt each; both workers acquired work and the audit chain verified |
| PostgreSQL and Garage restart | Rows, queued state, audit head and all six recorded private-artifact digests survived |
| Backup and fresh-destination restore | Actual `pg_dump`/`pg_restore` restored eight tables into a fresh database; the shipped S3 archive tool restored six objects into a fresh bucket; counts, states, audit head and hashes matched |
| Restored authority | The recovery lock inhibited both grants; the queued task was blocked with zero attempts; changed database/bucket placement invalidated the old qualification; authorized private operator reads remained available |

The evidence and its development qualification are included in `STATE-INTEGRATION.json`. All processes owned by this state exercise were stopped afterward. These observations establish the recorded single-host behavior, not HA, power-loss durability or production network enforcement. The real-model run used SQLite and filesystem artifacts; this PostgreSQL/S3 run used synthetic execution. They are separate complete application workflows, not one observed model/container/cluster combination.

The Python wheel also built successfully, and its required backend, CLI, native-service and local console assets were verified against the current source. The deployment and state records distinguish packaging, CLI parsing and rendering from executing containers or enforcing a cluster's policies.

## Deployment-specific work that remains

No actual Docker image build, Compose container lifecycle, Kubernetes reconciliation, GPU scheduling, admission controller or Cilium packet/DNS enforcement was observed here. No production data or production Kubernetes API was accessed. Those claims require an installation on the selected target.

The concrete target inputs are its repository or release artifact, cluster context or private host, approved image/model digests, storage and secret delivery, ingress identity, exact Kubernetes API addresses, tool peers and workload acceptance criteria. The Kubernetes installer requires an explicit context and cross-checks the relevant declared bindings; it does not infer or contact an unrelated environment.

Enforced mode requires deployment evidence with passing `runtime-identity`, `network-isolation`, `receipt-boundary`, `recovery` and `task-competence` checks. The built-in synthetic exercise cannot qualify an enforced or real-model deployment. A signature authenticates the trusted issuer's scoped evidence and binding; it is not hardware attestation or proof that a policy has actually been enforced.

## Material operating boundaries

The reference chart provisions one configured worker pool and one optional fixed native agent per release. The core has compute references and an external agent HTTP interface, but it is not a generic controller that deploys arbitrary external products from a capability label. KServe, kagent, ToolHive and other estate controllers remain concrete integration work if adopted.

The native service keeps its execution ledger separate from an optionally provisioned read-only input workspace. It receives no platform database, artifact-store, operator, caller or signing credentials. Its service boundary is shared across its tasks; it is not a distinct kernel for every caller. `max_steps` governs the bounded tool harness. Native execution uses the service and job time bounds; Sovite does not impose a native CLI turn count.

Current authority is checked at admission, mediated outbound requests, lease renewal and release. Native CLI-internal provider requests do not individually invoke the worker callback. Cancellation and network/service controls bound that interval. Revocation cannot retract data already sent or undo an external effect, and uncertain effectful work is not blindly replayed.

The operator role deliberately has private result access. The shipped identity model separates operators and named callers; it does not implement enterprise SSO, per-object operator delegation or independently isolated tenants. Existing browser sessions survive operator-token rotation until logout, expiry or administrative removal of their records.

The bundled PostgreSQL and Garage profiles are single-instance reference services. Persistence is implemented; redundant infrastructure and its availability guarantees remain a target decision. S3 and the database do not share a transaction, so an interrupted finalization can leave an unreferenced private object. Audit hashing detects inconsistency relative to the stored head, while a full database/history rewrite requires independent checkpoints to detect.

These boundaries are described in `SECURITY.md` and `OPERATIONS.md`, with the actual responsible components and commands.

## Start and assess the complete installation

From the extracted source directory, the complete local application starts with:

```sh
python3 scripts/local_platform.py init
python3 scripts/local_platform.py start
```

This profile uses explicit synthetic inference with the full application and durable operating workflow. Its generated operator credential is retained in the private local installation file, not printed or bundled into the release.

The private Compose profile provisions the real serving and native components using the operations guide. Kubernetes installation starts from `config/platform.kubernetes.yaml` and `deploy/examples/reference-values.yaml`, then binds the actual target values and public trust through `scripts/install_kubernetes.py`. Keep the same application and operating workflows while changing infrastructure bindings.

Assess representative incident delegation, scoped Kubernetes diagnostics, native code review, package/model replacement, cancellation, host replacement, backup/restore and routine operator work. Remove components later when their observed operational contribution does not justify their cost. The release already supplies the full reference system in which to make that decision.

## Release contents and name

**Sovite Platform** is the working name. It emphasizes fitting the independently managed parts into a useful operating system and covers the broader scope of this release. It succeeds the earlier Tulli flow-contract proposal. Public branding and package-name availability have not been treated as cleared.

The source archive includes application code, local assets, configuration/schema, locked Python dependency versions, container and Helm definitions, installer/recovery scripts, tests, operating documentation and scoped evidence records. `RELEASE-MANIFEST.json` records each included source file's SHA-256 and size. Runtime credentials, signing private keys, databases, private artifacts, model weights, downloaded third-party binaries and internal browser screenshots are excluded.
