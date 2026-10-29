# Sovite Platform

**Provision a model and agent platform across private and public boundaries, with explicit data classes, authority and independently configurable models, harnesses, agents, compute and state.**

Sovite provides the control plane, operator console, durable work queue, configuration contracts, classified results, scoped result access, caller-bound receipts, qualification records and operational controls. It adopts Ollama for local inference, the native Codex CLI for the isolated coding harness, PostgreSQL for deployment state, and Garage or another S3-compatible service for private artifacts.

The installation is the unit of assessment. The release includes a usable console and operating workflows, deployment profiles and recovery tools. The synthetic adapter provides repeatable exercises of the same application; real inference has separate configuration and cannot silently fall back to it.

The name is provisional. Sovite refers to the fitting between parts. This release succeeds the earlier Tulli flow-contract proposal and broadens the product to the complete operating platform.

## Included workflows

- Register independent models, harnesses, agent packages, tools, destinations and compute pools through a validated configuration bundle.
- Submit classified work from the operator console or a native caller through MCP.
- Bind each task to its exact configuration generation and compute pool; inspect queue state, attempts, cancellations and results.
- Keep artifacts in approved storage; release content only under the installed output class, result contract and current reader authority. Reference contracts preserve receipt-only behavior.
- Inhibit a grant immediately, stop new dispatch, recheck current authority during execution and prevent release after revocation.
- Validate and activate configuration revisions, issue signed qualifications, inspect their actual scope and expiry, and verify the audit chain.
- Install on a local Python environment, Docker Compose or Kubernetes. Export operational state, back up the Compose installation and restore with all grants inhibited.

## Start the complete local installation

Python 3.12 is the reference runtime. Run from the extracted repository root:

```sh
python3 scripts/local_platform.py init
python3 scripts/local_platform.py start
```

The local initializer runs the explicit database migration before its first
platform initialization. Runtime startup only checks schema compatibility and
refuses a fresh or older database until `sovite db migrate` completes.

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

Compose `up` starts PostgreSQL and Garage, runs `sovite db migrate` in a
one-shot control container, then starts the runtime services. It refuses to
migrate while control, worker, or native writer services are running.

`config/platform.ollama.yaml` contains real bindings for a Qwen3 4B analysis agent and a native Codex agent through Ollama's Responses endpoint. The initializer persists the native overlay selection for later build and lifecycle commands. Add `--workspace /path/to/approved-input` at initialization to mount a selected source snapshot read-only; no host input directory is mounted by default. See [Operations](docs/OPERATIONS.md) for workspace, GPU and deployment options. A local model outage queues or fails the request under its existing grant; it never redirects private data to a cloud provider.

The operator console can be used before a grant is qualified. Dispatch remains blocked until a trusted qualification for the current bundle and runtime placement is present. The built-in synthetic assessment cannot qualify a real or enforced installation. [Qualification and security](docs/SECURITY.md) explains the evidence contract and how to prepare a deployment qualification.

## Kubernetes and GitOps

The Helm chart includes the control plane, worker, PostgreSQL, Garage, Ollama, optional native harness, persistence, probes, resource requests and limits, scoped Kubernetes tool RBAC, default-deny networking and exact DNS rules for Cilium.

```sh
.venv/bin/python scripts/validate_deployment.py --output release.rendered.yaml
```

Use `scripts/install_kubernetes.py` with an explicit context, the platform bundle, reviewed Helm values, the runtime secret input and qualification public keys. `--render-only` produces reviewable manifests without contacting a cluster. The full reference values require the actual Kubernetes API CIDRs and installation endpoints. See [Operations](docs/OPERATIONS.md) for the complete invocation and model staging steps.

Before a Helm rollout, stop old control/worker writers and run the candidate image's `sovite db migrate` once with the migration-role Secret. The chart has no migration Job; [Operations](docs/OPERATIONS.md#database-migrations-and-hosted-startup) includes a concrete Job manifest and wait/check sequence. Runtime pods use the runtime database role and check schema compatibility without DDL.

`config/platform.kubernetes.yaml` and `deploy/examples/reference-values.yaml` describe the full reference installation. The default chart can also use externally managed PostgreSQL and S3. Enforced mode refuses mutable workload image tags, missing qualification, insecure operator access and unrestricted network exceptions. Image pins must be actual resolved digests; `scripts/lock_images.py` resolves registry manifests.

## Connect an existing native caller

Merge the stanza in `config/codex.mcp.toml` into the caller's user-level Codex configuration, set its scoped caller-token environment variable, and use the deployed MCP URL. The cloud caller retains its native model, tools and harness. Sovite exposes delegated work under installed grants and data-class boundaries.

The caller sees `list_candidates`, `begin_task`, `task_status` and `read_result`. Discovery lists only caller-visible grants; detailed metadata requires the installed discovery-class clearance. Its labelled observations are not reservations or live account/capacity checks. Preferences stay with the coordinator. A receipt is an independent server-created identifier, not a bearer capability. A `reference` contract returns receipt/status only; `class-aware` permits output only within the caller's clearance. Explicit `result_readers` give coordinator and reviewer identities scoped content access without operator-wide authority. Every content release rechecks current producing-job authority and artifact integrity. Operator tokens are rejected by MCP. See [caller discovery and attempt lineage](docs/CALLER-DISCOVERY.md).

## Repository-session status

An internal pre-launch workspace/evidence foundation now validates a closed
`sovite-repository-session/v1` packet against a trusted owner binding, creates
an exact-base detached Git worktree, and records candidate evidence. **There is
no released repository-session API or worker launch capability.** This
foundation is not wired to job admission, the execution lease API, native
provider launchers, or merge authority, and it does not claim a hard process
sandbox. See the [repository-session workspace decision record](docs/plans/2026-10-25-repository-session-workspace-foundation.md)
and [the broader portable-workflow plan](docs/plans/2026-10-24-portable-agent-workflows.md).

## Configuration and runtime boundaries

| Layer | Configuration | Implementation |
| --- | --- | --- |
| Model and serving binding | `models` | Local/gateway bindings and explicit Codex/Claude subscription or OpenCode Go routes; content identity and hosted service identity remain distinct |
| Harness package | `harnesses` | Fixed native Codex, Claude Code and OpenCode services, bounded tool harness, external agent HTTP adapter and explicit synthetic adapter |
| Agent package | `agents` | Harness/model references, instructions, tool allowlist and a step limit for the bounded tool harness |
| Compute placement | `compute` | Worker pool identity and local/Kubernetes placement |
| Data placement | `destinations`, `data_classes` | Clearance checks across inference, execution, tools, state and bounded telemetry |
| Authorization | `grants` | Caller, class, agent, compute, return mode, timeout and outage behavior |
| Durable operating state | Database and artifact backend | Jobs, leases, configurations, qualifications, sessions, inhibits, audit and private results |

Configuration and runtime controls have separate write paths. A reviewed bundle determines desired authority; an operator inhibit only reduces that authority. Clearing an inhibit does not override a disabled or unqualified grant. Replacing a bundle blocks work bound to the superseded generation.

## Delivery definitions and data boundaries

An omitted job or workflow `input_class` selects the highest installed class. Inputs at or above the restricted threshold require private-boundary inference. Lower classes can use public services only where the grant and destination permit them. Each destination declares a private/public boundary; a high clearance does not make a public subscription provider private.

Provider-owned delivery workflows bind requirements and session-policy revisions to installed role grants. The binding checks every role and declared transfer before admission. Private-to-public transfer or class reduction requires an installed output contract and a recorded approval over the exact source artifact and released bytes. The provider controller still owns discovery, planning, session scheduling, acceptance review, semantic repair and merge decisions. Vuoro consumes work/authority/evidence records; the definitions do not reinstate a Vuoro runtime recipe compiler or implement a delivery scheduler. Direct mode supports a one-session path. See [delivery workflows](docs/WORKFLOWS.md) and [the API contract](docs/API_CONTRACT.md).

## Subscription models and OpenCode Go

The native routes use explicit operator-provisioned authentication and exact configured model selection. They never discover ambient credentials or switch to an API-key account when a subscription route fails.

| Route | Native capability | Authentication and selection |
| --- | --- | --- |
| Codex ChatGPT subscription | `native-codex` | `codex-subscription` model with `identity_protocol: native-subscription`; harness `authentication: {mode: subscription, credential_file_env: SOVITE_CODEX_AUTH_FILE}` selects one protected ChatGPT auth.json. |
| Claude subscription | `native-claude` | `claude-subscription` model with `identity_protocol: native-subscription`; harness `authentication: {mode: subscription, credential_env: SOVITE_CLAUDE_OAUTH_TOKEN}` selects its OAuth token. |
| OpenCode Go | `native-opencode` | `opencode-go` model with `identity_protocol: provider-service`, `model: deepseek-v4.1-flash`, `endpoint: https://opencode.ai/inference/go/openai/v1`, `wire_api: chat` and an explicit Go-key `api_key_env`. Native OpenCode selects `opencode-go/deepseek-v4.1-flash`. |

All three use public cloud destinations and declared service/model identity; they do not claim immutable hosted weights. Enforced v2 installations retain image pins, destination policy and qualification requirements. The narrower service-only hosted envelope remains restricted to internal model-gateway connections. Go also supports the bounded `tool-agent` path with Sovite's explicit tool declarations, per-job session header and an identifying User-Agent.

New native launchers are answer-only: fresh home/XDG/task directories, denied native tools, explicit models and no ambient API keys, user settings or workspaces. OpenCode requires v2. Codex preserves a refreshed credential file atomically, including failure and cancellation; its local sidecar lock requires cross-host serialization and coordinated rotation by the operator. Native process-group cancellation also terminates descendants surviving their leader. These controls do not replace deployment isolation or qualification of machine-wide policy.

Set `SOVITE_HARNESS_AGENT_ID`, `SOVITE_BUNDLE_PATH` and the separate `SOVITE_HARNESS_TOKEN` on each fixed native service. A hosting installation also supplies the identical `SOVITE_HOSTING_ENVELOPE`. The reference chart provisions one optional fixed native agent per release, so a multi-role package needs separately provisioned services or releases. The example is a configuration contract, not a claim that accounts, credentials, executable images or egress have been provisioned.

Validation uses synthetic credentials/responses and real local subprocesses for output bounds and cancellation. No live subscription/account request or installed native CLI qualification is claimed. Upstream contracts: [Codex authentication](https://developers.openai.com/codex/auth/ci-cd-auth), [Claude Code authentication](https://code.claude.com/docs/en/authentication), [OpenCode Go](https://opencode.ai/v2/docs/console/go/) and [OpenCode v2 configuration](https://opencode.ai/v2/docs/config/).

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

Release 0.1.17 sends hosted compilation and activation through the authenticated control API, without local database access. It bounds transport errors and preserves uncertain activation outcomes.

Python owner release 0.1.16 adds a local-only task/receipt catalog and rejects
explicitly truncated model responses. See [the integration contract](docs/integrations/vuoro-private-workflow.md)
for identity binding, private artifact separation and the bounded C0 evaluation.
This Python release does not change published deployment image tags or hosted gates.
