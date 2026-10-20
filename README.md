# Sovite Platform

**Provision a complete private model and agent platform, while keeping models, serving, harnesses, agent definitions, compute and state independently configurable.**

Sovite provides the control plane, operator console, durable work queue, configuration contracts, private results, caller-bound receipts, qualification records and operational controls. It adopts Ollama for local inference, the native Codex CLI for the isolated coding harness, PostgreSQL for deployment state, and Garage or another S3-compatible service for private artifacts.

The installation is the unit of assessment. The release includes a usable console and operating workflows, deployment profiles and recovery tools. The synthetic adapter provides repeatable exercises of the same application; real inference has separate configuration and cannot silently fall back to it.

The name is provisional. Sovite refers to the fitting between parts. This release succeeds the earlier Tulli flow-contract proposal and broadens the product to the complete operating platform.

## Included workflows

- Register independent models, harnesses, agent packages, tools, destinations and compute pools through a validated configuration bundle.
- Submit private work from the operator console or the receipt-only MCP endpoint used by a native cloud caller.
- Bind each task to its exact configuration generation and compute pool; inspect queue state, attempts, cancellations and results.
- Keep complete outputs in private storage and return only an opaque receipt plus a bounded status to callers.
- Inhibit a grant immediately, stop new dispatch, recheck current authority during execution and prevent release after revocation.
- Validate and activate configuration revisions, issue signed qualifications, inspect their actual scope and expiry, and verify the audit chain.
- Install on a local Python environment, Docker Compose or Kubernetes. Export operational state, back up the Compose installation and restore with all grants inhibited.

## Start the complete local installation

Python 3.12 is the reference runtime. Run from the extracted repository root:

```sh
python3 scripts/local_platform.py init
python3 scripts/local_platform.py start
```

Open **http://127.0.0.1:8080**. The generated operator credential is in `.state/local/installation.json`; the script does not print credentials. The local profile runs the same operator application, API, independent worker, signed development qualification, durable SQLite database and private filesystem artifacts. Inference is explicitly synthetic in this profile.

```sh
python3 scripts/local_platform.py status
python3 scripts/local_platform.py stop
```

Stopping retains state. To assess durable recovery, stop and restart the installation, then inspect its existing jobs, results and audit history.

## Install real private inference and native harnesses

The Compose profile provisions the API and console, a separate worker, PostgreSQL, Garage and Ollama. Selecting the Ollama profile also selects and retains the independently isolated Codex service.

```sh
python3 scripts/installation.py compose-init --profile ollama
python3 scripts/installation.py build
python3 scripts/installation.py keys-init
python3 scripts/installation.py up
python3 scripts/installation.py stage-model
```

`config/platform.ollama.yaml` contains real bindings for a Qwen3 4B analysis agent and a native Codex agent through Ollama's Responses endpoint. The initializer persists the native overlay selection for later build and lifecycle commands. Add `--workspace /path/to/approved-input` at initialization to mount a selected source snapshot read-only; no host input directory is mounted by default. See [Operations](docs/OPERATIONS.md) for workspace, GPU and deployment options. A local model outage queues or fails the request under its existing grant; it never redirects private data to a cloud provider.

The operator console can be used before a grant is qualified. Dispatch remains blocked until a trusted qualification for the current bundle and runtime placement is present. The built-in synthetic assessment cannot qualify a real or enforced installation. [Qualification and security](docs/SECURITY.md) explains the evidence contract and how to prepare a deployment qualification.

## Kubernetes and GitOps

The Helm chart includes the control plane, worker, PostgreSQL, Garage, Ollama, optional native harness, persistence, probes, resource requests and limits, scoped Kubernetes tool RBAC, default-deny networking and exact DNS rules for Cilium.

```sh
.venv/bin/python scripts/validate_deployment.py --output release.rendered.yaml
```

Use `scripts/install_kubernetes.py` with an explicit context, the platform bundle, reviewed Helm values, the runtime secret input and qualification public keys. `--render-only` produces reviewable manifests without contacting a cluster. The full reference values require the actual Kubernetes API CIDRs and installation endpoints. See [Operations](docs/OPERATIONS.md) for the complete invocation and model staging steps.

`config/platform.kubernetes.yaml` and `deploy/examples/reference-values.yaml` describe the full reference installation. The default chart can also use externally managed PostgreSQL and S3. Enforced mode refuses mutable workload image tags, missing qualification, insecure operator access and unrestricted network exceptions. Image pins must be actual resolved digests; `scripts/lock_images.py` resolves registry manifests.

## Connect an existing native caller

Merge the stanza in `config/codex.mcp.toml` into the caller's user-level Codex configuration, set its scoped caller-token environment variable, and use the deployed MCP URL. The cloud caller retains its native model, tools and harness. Sovite is a delegated private work capability exposed through MCP.

The caller sees `begin_task` and `task_status`. A receipt is an independent server-created identifier, bound to the authenticated caller. It cannot be exchanged for a private result. Operator credentials are rejected by the public MCP surface; operator-submitted jobs stay in the private operator workflow.

## Configuration and runtime boundaries

| Layer | Configuration | Implementation |
| --- | --- | --- |
| Model and serving binding | `models` | Ollama and OpenAI-compatible endpoints; separate protocol selection and model identity |
| Harness package | `harnesses` | Native Codex service, bounded diagnostic harness, external agent HTTP adapter, explicit synthetic adapter |
| Agent package | `agents` | Harness/model references, instructions, tool allowlist and a step limit for the bounded tool harness |
| Compute placement | `compute` | Worker pool identity and local/Kubernetes placement |
| Data placement | `destinations`, `data_classes` | Clearance checks across inference, execution, tools, state and bounded telemetry |
| Authorization | `grants` | Caller, class, agent, compute, return mode, timeout and outage behavior |
| Durable operating state | Database and artifact backend | Jobs, leases, configurations, qualifications, sessions, inhibits, audit and private results |

Configuration and runtime controls have separate write paths. A reviewed bundle determines desired authority; an operator inhibit only reduces that authority. Clearing an inhibit does not override a disabled or unqualified grant. Replacing a bundle blocks work bound to the superseded generation.

## Verification and current readiness

Run the contract and lifecycle suite after installing the development dependencies:

```sh
.venv/bin/python -m pytest
.venv/bin/python scripts/validate_deployment.py
```

See [Release readiness](docs/RELEASE-READINESS.md) for what was actually exercised, the exact local inference/runtime versions, and what remains dependent on the target cluster. Configuration rendering, successful model responses, qualification signatures and live network isolation are separate facts; the console and evidence records preserve that distinction.

The release is a complete implementation for whole-platform assessment. It does not constitute a certification of a deployment that has not been installed and observed. In particular, production data access depends on the target's real identities, network rules, storage placement and workload qualification.

## Repository guide

- [Platform specification](docs/PLATFORM-SPEC.md): product boundary, architecture and whole-platform assessment.
- [Operations](docs/OPERATIONS.md): installation, staging, activation, backup, restore and export.
- [Security and qualification](docs/SECURITY.md): trusted components, enforcement duties and evidence.
- [API contract](docs/API_CONTRACT.md): operator API, receipts and the external native-harness contract.
- [Upstreams](docs/UPSTREAMS.md): adopted components, pinned references and explicit extension boundaries.

No production credentials, private keys, runtime databases or model weights are included in the source distribution.

## Vuoro private workflow integration

Python owner release 0.1.16 adds a local-only task/receipt catalog and rejects
explicitly truncated model responses. See [the integration contract](docs/integrations/vuoro-private-workflow.md)
for identity binding, private artifact separation and the bounded C0 evaluation.
This Python release does not change published deployment image tags or hosted gates.
