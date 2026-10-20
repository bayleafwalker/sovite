# Operating Sovite Platform

The installed platform is the unit of assessment: the console, control API, durable queue, independent workers, private results, public receipts, authority controls, qualification records and recovery behavior operate together. The synthetic profile exercises that entire workflow with labeled synthetic inference. The Ollama and native profiles use actual serving and harness processes. Production authority is enabled only for a concrete deployment whose evidence applies to its current generation and runtime binding.

## Installation profiles

| Profile | Processes and persistence | Intended use |
|---|---|---|
| Local | Full API and console, independent worker, SQLite, filesystem artifacts | Run and inspect the platform without container or cluster prerequisites. Inference is explicitly synthetic. |
| Compose | Full API/console and worker, PostgreSQL, Garage, Ollama, separate model staging network; optional isolated native Codex service | Provision the complete installation on one private host and replace infrastructure bindings deliberately. |
| Kubernetes | Helm-managed workloads, persistent claims, probes, service identities, ingress, default-deny networking, exact DNS allowances, optional namespaced diagnostic RBAC | Integrate the platform with a private cluster and standard configuration delivery. |

The bundled PostgreSQL and Garage instances have persistent storage but one replica. Their failure tolerance follows that topology. Use separately operated PostgreSQL and S3 services when the site's recovery and availability objectives require redundancy; the control/worker interfaces stay the same.

## Start the local installation

From the repository root:

```bash
python3 scripts/local_platform.py init
python3 scripts/local_platform.py start
python3 scripts/local_platform.py status
```

The installer creates an isolated Python environment, installs the locked dependencies and package, creates distinct operator/caller credentials, initializes durable state, runs the synthetic assessment and signs that assessment for the local generation. The console is at `http://127.0.0.1:8080`. Read the operator token from `.state/local/installation.json` when signing in; it is not printed or placed in browser localStorage. Use `init --port 8083` for an alternate port. An already provisioned virtual environment can be selected with `--venv PATH --skip-dependencies`.

`python3 scripts/local_platform.py stop` stops the managed worker and API while preserving their database and artifacts. PID start-time checks avoid signaling an unrelated process after PID reuse. Runtime logs are kept in `.state/local/`; they belong inside the private operating boundary.

If setup is interrupted, `python3 scripts/local_platform.py init --resume` completes assessment and qualification using the original credentials, signing identity and durable state. A partial signing key pair causes an explicit failure. Repeated assessment records are preserved. The signing key path is supplied only to key generation and qualification commands, rather than being passed to the long-running API or worker.

## Provision the Compose installation

Install Docker with the Compose plugin, then run:

```bash
python3 scripts/installation.py compose-init --profile demo
python3 scripts/installation.py build
python3 scripts/installation.py keys-init
python3 scripts/installation.py up
python3 scripts/installation.py assess-synthetic .state/evidence/initial.json
python3 scripts/installation.py qualify .state/evidence/initial.json --profile local
python3 scripts/installation.py health
```

Initialization writes private credentials to `.env` without printing them. Existing configuration is preserved rather than silently overwritten. API and worker containers receive public qualification keys; only the short-lived signing command mounts the private key. Initial dispatch remains blocked until the current configuration and runtime binding have a valid qualification record.

For local inference, select `--profile ollama` when initializing a fresh installation. Set `SOVITE_STAGE_MODEL` to the approved model tag, then run `python3 scripts/installation.py stage-model`. Model staging stops the inference process, worker and any provisioned native service, downloads into the model volume through a separate staging network, and restarts only services that were previously running. The inference service itself remains on an internal network with cloud features disabled. Resolve the actual model identity and collect the appropriate deployment evidence before qualification. The built-in synthetic assessment deliberately rejects real model profiles.

The Ollama initializer selects the native Codex overlay automatically and persists `SOVITE_COMPOSE_OVERRIDES=deploy/compose/native-codex.yaml` in `.env`; later build/up/backup commands reload that selection. The demo initializer selects no native service. The variable accepts multiple files separated by the host's path separator. Add `deploy/compose/gpu.yaml` to the saved selection when the host has the NVIDIA container runtime and a suitable GPU. An explicitly set shell variable overrides the saved selection, including an intentionally empty value. The native service has its own private ledger and scratch volume and a fixed agent identity. It receives its service token and model/tool bindings, not database, object-store or operator credentials. No host Docker socket, general host filesystem or ambient cloud credentials are mounted.

### Provide approved native input files

By default, native execution receives a task and isolated scratch space. To inspect files, stage a task-specific directory containing the approved source snapshot, then initialize with `--profile ollama --workspace /absolute/path/to/review-input`. The installer saves that path, selects `deploy/compose/native-workspace.yaml`, and mounts it read-only at `/workspace/input`. The source directory must already exist; Compose is instructed not to create a missing bind source. Use a staged source snapshot or explicitly selected input files, leaving the platform's credentials, state and signing keys outside it. For example, a source repository can be staged from a reviewed commit using standard Git tooling:

```bash
mkdir -p workspaces/review-input
git -C /path/to/source archive --format=tar --output=/absolute/path/to/review-input.tar REVIEWED_COMMIT
tar -xf /absolute/path/to/review-input.tar -C workspaces/review-input
python3 scripts/installation.py compose-init --profile ollama --workspace workspaces/review-input
```

For an existing installation, add the workspace overlay to the saved overlay list and set `SOVITE_NATIVE_WORKSPACE_SOURCE` to the approved input directory. Reconcile the native service after changing the mount. The adapter receives `SOVITE_NATIVE_WORKSPACE=/workspace/input`. Its writable execution ledger remains in `native-harness-state`, separate from this input. File access still depends on the chosen native sandbox, model and installed host being compatible; readiness alone does not establish that compatibility.

The console port is bound to loopback by default. Put a private TLS gateway in front of it for remote access, set the exact public origin and enable secure cookies. The Compose network arrangement is useful isolation between services; it is not advertised as the same evidence as a qualified cluster's Cilium/DNS policy.

## Provision Kubernetes

Use `config/platform.kubernetes.yaml` with `deploy/examples/reference-values.yaml` for the complete reference profile. The reference enables the isolated native service and namespaced Kubernetes diagnostics. The site's values must supply the actual API server CIDRs, published control/native image references, private HTTPS origin, gateway selectors and storage classes where necessary. The empty API CIDR list intentionally fails rendering until the target is bound; the installer never guesses a production API address or grants cluster-admin.

Service names with `fullnameOverride: sovite` are `sovite-control:8080`, `sovite-ollama:11434`, `sovite-native-harness:8090`, `sovite-postgres:5432` and `sovite-garage:3900`. The supplied bundle expects the `sovite` namespace. Keep bundle destinations, service names, RBAC namespace and network declarations aligned.

This reference chart provisions one configured Kubernetes worker pool and one optional native service with one fixed agent identity per release. Every enabled grant in that release must use the provisioned pool. The installer rejects a local compute declaration in a Kubernetes installation. The core compute references and HTTP service contracts allow additional deployments to be integrated, but this release does not contain a generic controller that reconciles arbitrary compute or harness packages.

Use a Python environment with the locked dependencies and Sovite package installed for Helm operations. The local bootstrap already creates this environment; an independent operator workstation can prepare it without starting a platform:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python scripts/installation.py credentials-init --bundle config/platform.kubernetes.yaml --origin https://sovite.example.internal
.venv/bin/python scripts/installation.py keys-init --local
.venv/bin/python scripts/installation.py kubernetes-secret .state/runtime-kubernetes.json
```

The credential initializer preserves an existing `.env`; reuse existing managed credentials deliberately instead of generating a second identity. `keys-init --local` creates the offline Ed25519 key pair without Docker. Keep the private key in operator custody. The renderer reads only the specifically declared public key filenames.

```bash
.venv/bin/python scripts/install_kubernetes.py --context YOUR_CONTEXT --namespace sovite --bundle config/platform.kubernetes.yaml --values deploy/examples/reference-values.yaml --values site.values.yaml --trust-dir .state/trust --render-only --render-output release.rendered.yaml
.venv/bin/python scripts/install_kubernetes.py --context YOUR_CONTEXT --namespace sovite --bundle config/platform.kubernetes.yaml --values deploy/examples/reference-values.yaml --values site.values.yaml --runtime-env .state/runtime-kubernetes.json --trust-dir .state/trust --render-output release.rendered.yaml
```

`--render-only` produces four artifacts without reading credentials or contacting the cluster: the complete reviewed YAML, merged `release.rendered.effective-values.yaml`, a `release.rendered.prerequisites.yaml` containing the namespace and public configuration/trust, and `release.rendered.review.json`. The review includes the actual bundle and Ed25519 public keys, all workload/network resources and required Secret references. It never contains the Secret's values.

The renderer computes a deterministic `deploymentBinding` from the canonical rendered resources, bundle generation, merged public values, chart file hashes, context, release and namespace. It normalizes only the fingerprint's self-reference. The chart injects the same `SOVITE_DEPLOYMENT_BINDING` into the control and worker environments. Changing network policy, image placement, public trust or another bound input changes the binding and invalidates previous qualification through the runtime settings fingerprint. An unchanged render has an unchanged binding. The installer also verifies that chart files and effective values have not changed before applying them.

Binding checks require identical bundle/deployment modes, matching pool and namespace, provisioned model/native endpoints, a consistent fixed native identity, correctly scoped tool RBAC, and matching state endpoints. Enforced harness digests must equal the deployed runtime/native image digests. An enabled grant that references a disabled service is rejected. External in-cluster model, harness and tool services require explicit `networkPolicy.modelPeers`, `harnessPeers` or `toolPeers` entries, each with exact hosts, namespace and pod selectors, and TCP ports. Those entries generate both network allowances and finite DNS queries. A model or harness served outside the cluster uses a `modelPeers` or `harnessPeers` entry with exact host CIDRs (`/32` or `/128`) instead of selectors; its hosts must be the IP literals inside those CIDRs, because a name could resolve elsewhere, and it generates no DNS allowance. A model's `api_key_env` must be listed in `modelCredentials`, which injects that key from the runtime Secret into the worker and native harness.

Applying requires an explicit context, an existing Cilium policy CRD, runtime credentials and the public trust files. When Kubernetes tools are enabled, the installer reads the selected cluster's `kubernetes` EndpointSlices and requires the configured API CIDRs to be exact `/32` or `/128` host addresses matching the observed endpoints and TCP port. Empty, broad or mismatched allowances fail before mutation. The installer delivers prerequisites and the runtime Secret, then waits for Helm reconciliation. Enforced mode requires digest references, HTTPS, secure cookies, qualification and the strict network profile, along with an enforced bundle containing actual runtime identities. Manifest binding records desired deployment state; live CNI behavior, admission controls, mutable cluster state and task competence still require deployment evidence.

Model weights must be staged into the Ollama PVC or supplied using `ollama.existingClaim`. The runtime does not have an Internet registry exception. Stop inference and jobs while replacing a mutable model cache, verify its resulting digest, and qualify the new binding before activating grants. External tool services remain their owner's responsibility: include argument sensitivity, tool result classification, endpoint identity and the tool service's own egress controls in the deployment evidence.

To provide native input files on Kubernetes, populate a separate approved PVC using the site's normal staging Job or artifact delivery mechanism, then set `nativeHarness.workspace.existingClaim` to that claim. An optional `nativeHarness.workspace.subPath` selects the staged snapshot directory. The chart mounts the selected content at `/workspace/input` with both the PVC and container mount marked read-only and sets `SOVITE_NATIVE_WORKSPACE` accordingly. It creates no `hostPath` and does not create an input PVC implicitly. The separately provisioned native ledger claim stays writable. Choose a storage access mode compatible with the staging and reader placement; complete staging before reconciling the native reader.

For GitOps, place the reviewed chart under the path referenced by `deploy/gitops/helmrelease.yaml`, connect an actual `GitRepository`, and publish the generated effective values as the `values.yaml` key of `sovite-effective-values`. Reconcile the generated public prerequisites and separately managed runtime Secret before the Helm release. Do not apply the full review YAML alongside Helm, since it also includes Helm-owned resources. Re-run binding and target API checks whenever site values or the chart change. Deliver runtime secrets with the site's existing secret management or encrypted Git workflow. The release example contains no live repository identity, credentials or assumption that Flux already exists.

## Qualification and current authority

The signing input contains the current generation, selected profile, evidence scope and named checks with `passed`, `failed` or `not_run` status. A signature over incomplete or failed evidence does not enable a grant. Enforced bundles additionally require deployment evidence covering runtime identity, network isolation, receipt boundaries, recovery and task competence. Signing is a privileged operating action; the runtime verifies trusted public keys and the current binding.

`sovite runtime-binding` reports the nonsecret runtime settings fingerprint used for signing. Run signing commands with the same database, artifact endpoint, origin, security settings and `SOVITE_DEPLOYMENT_BINDING` as the installation. The Helm review records the latter value. Configuration changes invalidate old evidence rather than inheriting a green status.

Operators can inhibit a grant immediately from the console. This blocks dispatch and public release and requests cancellation of active work. Resume clears the runtime inhibit only when the current desired grant and qualification still authorize it. A blocked or cancelled task can retain a private artifact for investigation; the public MCP caller receives a fixed receipt/status projection and never a private result URL or provider task ID.

## Backup, restore and export

For the Compose installation:

```bash
python3 scripts/installation.py backup backups/sovite-2026-10-19.tar.gz
python3 scripts/installation.py export backups/operator-inventory.json
python3 scripts/installation.py restore backups/sovite-2026-10-19.tar.gz --accept-replace
```

Backup stops all configured writers, including the native service, before taking a PostgreSQL custom-format dump and logical S3 artifact export. It captures the desired bundle, public trust and native service cache if provisioned, computes checksums, creates a mode-0600 archive and restarts only services that were running before the backup. Symbolic links in copied state are rejected. A consistent backup has an availability cost while writers are stopped.

Restore verifies archive paths and checksums before replacing the database and restoring objects. It preserves current desired configuration and trusted public keys, applies a recovery inhibit to every grant, and verifies the audit chain before restarting prior writers. Failed restoration leaves writers stopped. Review the current generation, qualification and outstanding actions before resuming grants; an old database must not silently revive older authority.

Backups contain private data and belong in approved encrypted backup storage. They exclude runtime credentials, the signing private key, model weights, separately staged native input snapshots and independently managed tool data. Preserve approved source snapshots in their source repository or the site's input storage recovery system. Back up the signing key separately through the site's key custody process. The operator export is a bounded API inventory, not a replacement for backup; it omits private result bodies and may be limited by API pagination.

For Kubernetes, use the database/object-store recovery systems and CSI snapshots selected by the site. Quiesce the control/worker/native deployments for a coordinated snapshot, include native state and configuration revisions, and run `sovite recovery-lock --reason ...` before allowing a restored installation to dispatch. The Compose archive script is not represented as a general cross-provider Kubernetes backup controller.

## Failure handling and removal

Model outages leave authorized jobs queued or retrying according to their grant. They do not invoke an undeclared cloud fallback. Durable leases let a worker recover abandoned work; non-idempotent or uncertain execution requires intervention instead of blindly repeating an action. The native service stores request identity separately from the worker queue and returns a cached completed result for a repeat of the same request.

`health/live` checks the API process; `health/ready` checks database and artifact access. Neither endpoint certifies model quality, GPU placement or information-flow policy. Use the console's worker, queue, inhibit, qualification and audit views to distinguish readiness from authorization.

`installation.py down` removes Compose services and networks while keeping named volumes. Do not use volume deletion as routine shutdown. Helm-managed stateful claims and the explicitly retained model/native claims require an intentional retention decision when uninstalling. To remove an unneeded component later, update its package bindings and grants, drain/inhibit its jobs, export retained state and then remove its deployment. The platform contracts and private artifacts remain inspectable outside the UI.

## Observed distribution validation

`DEPLOYMENT-VALIDATION.json` records the checks actually performed for this delivery. The local installation was bootstrapped, assessed, qualified and exercised through its real API and independent worker, including an MCP request, idempotency, private artifact access, a complete stop/restart, persistence, audit verification and clean shutdown. Helm was used to render and bind the complete reference release and reject invalid configurations. The official Compose CLI parsed and merged the synthetic, native and GPU profiles. Container builds, Docker networking, Helm reconciliation and live Kubernetes network isolation are distinct checks; the report does not claim those took place.

## Site integration and operator-held signing

The chart supports `ollama.runtimeClassName` for an installed GPU runtime and
`ollama.gpuOverheadBytes` to reserve VRAM on shared devices. Resource requests
still determine scheduling; a time-sliced GPU allocation does not promise
exclusive memory or throughput. Measure the loaded model's actual CPU/GPU split.

For Cilium Gateway API, `networkPolicy.controlIngress.ciliumGateway: true`
permits the `ingress` identity to reach only the control service on TCP 8080.
`networkPolicy.ciliumAPIServer: true` permits only the worker to reach the
`kube-apiserver` identity on the configured API port. Keep explicit API addresses
and run the installer's live endpoint validation. Kubernetes RBAC independently
restricts the namespace and resource verbs. Test both allowed and denied paths.

`nativeHarness.workspace.configMap` can provide a small approved read-only
fixture instead of a PVC. The chart rejects simultaneous ConfigMap/PVC sources.
`persistenceLabels` on `postgres`, `garage`, `nativeHarness` and `ollama` lets a
site enroll volumes in its existing backup system. Verify the resulting live
volume membership; cache files and durable execution ledgers have different
recovery requirements.

`scripts/qualify_kubernetes.py` signs from the operator workstation without
copying a private key into a workload. Open a loopback PostgreSQL port-forward,
then supply the explicit context, namespace, bundle, rendered effective values,
private runtime JSON, signing/public key paths, evidence, and local database
port. It verifies the live runtime fingerprint and active generation before
signing. The database connection uses the local tunnel while the signature binds
the installation's original database address. A qualification's scope must state
what was actually observed; commissioning evidence is not production evidence.
