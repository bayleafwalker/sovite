# Operating Sovite Platform

The installed platform is the unit of assessment: the console, control API, durable queue, independent workers, classified results, scoped result access, authority controls, qualification records and recovery behavior operate together. The synthetic profile exercises that entire workflow with labeled synthetic inference. The Ollama and native profiles use actual serving and harness processes. Production authority is enabled only for a concrete deployment whose evidence applies to its current generation and runtime binding.

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

On Kubernetes, Codex's read-only sandbox works only with `nativeHarness.sandbox.userNamespace: true` and `seccompLocalhostProfile` naming `deploy/seccomp/sovite-codex-sandbox.json` installed on every node that can run the harness, under the kubelet seccomp root (for example `profiles/sovite-codex-sandbox.json`). Without them every Codex command fails at `bwrap`, and the agent cannot read its workspace. See the security model for the trade-off.

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

Binding checks require identical bundle/deployment modes, matching pool and namespace, provisioned model/native endpoints, a consistent fixed native identity, correctly scoped tool RBAC, and matching state endpoints. Enforced harness digests must equal the deployed runtime/native image digests. An enabled grant that references a disabled service is rejected. External in-cluster model, harness and tool services require explicit `networkPolicy.modelPeers`, `harnessPeers` or `toolPeers` entries, each with exact hosts, namespace and pod selectors, and TCP ports. Those entries generate both network allowances and finite DNS queries. A model or harness served outside the cluster uses a `modelPeers` or `harnessPeers` entry with exact host CIDRs (`/32` or `/128`) instead of selectors; its hosts must be the IP literals inside those CIDRs, because a name could resolve elsewhere, and it generates no DNS allowance. A model's `api_key_env` must be listed in `modelCredentials`, which injects that key from the runtime Secret into the worker and native harness. A model host outside the cluster must be reached over HTTPS. A self-signed or private certificate is pinned by name in `modelTrust` and selected with the model's `tls_trust`; that model then trusts only that certificate, in both the bounded adapter and the native Codex CLI (`CODEX_CA_CERTIFICATE`).

Applying requires an explicit context, an existing Cilium policy CRD, runtime credentials and the public trust files. When Kubernetes tools are enabled, the installer reads the selected cluster's `kubernetes` EndpointSlices and requires the configured API CIDRs to be exact `/32` or `/128` host addresses matching the observed endpoints and TCP port. Empty, broad or mismatched allowances fail before mutation. The installer delivers prerequisites and the runtime Secret, then waits for Helm reconciliation. Enforced mode requires digest references, HTTPS, secure cookies, qualification and the strict network profile, along with an enforced bundle containing actual runtime identities. Manifest binding records desired deployment state; live CNI behavior, admission controls, mutable cluster state and task competence still require deployment evidence.

Model weights must be staged into the Ollama PVC or supplied using `ollama.existingClaim`. The runtime does not have an Internet registry exception. Stop inference and jobs while replacing a mutable model cache, verify its resulting digest, and qualify the new binding before activating grants. External tool services remain their owner's responsibility: include argument sensitivity, tool result classification, endpoint identity and the tool service's own egress controls in the deployment evidence.

To provide native input files on Kubernetes, populate a separate approved PVC using the site's normal staging Job or artifact delivery mechanism, then set `nativeHarness.workspace.existingClaim` to that claim. An optional `nativeHarness.workspace.subPath` selects the staged snapshot directory. The chart mounts the selected content at `/workspace/input` with both the PVC and container mount marked read-only and sets `SOVITE_NATIVE_WORKSPACE` accordingly. It creates no `hostPath` and does not create an input PVC implicitly. The separately provisioned native ledger claim stays writable. Choose a storage access mode compatible with the staging and reader placement; complete staging before reconciling the native reader.

For GitOps, place the reviewed chart under the path referenced by `deploy/gitops/helmrelease.yaml`, connect an actual `GitRepository`, and publish the generated effective values as the `values.yaml` key of `sovite-effective-values`. Reconcile the generated public prerequisites and separately managed runtime Secret before the Helm release. Do not apply the full review YAML alongside Helm, since it also includes Helm-owned resources. Re-run binding and target API checks whenever site values or the chart change. Deliver runtime secrets with the site's existing secret management or encrypted Git workflow. The release example contains no live repository identity, credentials or assumption that Flux already exists.

## Qualification and current authority

The signing input contains the current generation, selected profile, evidence scope and named checks with `passed`, `failed` or `not_run` status. A signature over incomplete or failed evidence does not enable a grant. Enforced bundles additionally require deployment evidence covering runtime identity, network isolation, receipt boundaries, recovery and task competence. Signing is a privileged operating action; the runtime verifies trusted public keys and the current binding.

`sovite runtime-binding` reports the nonsecret runtime settings fingerprint used for signing. Run signing commands with the same database, artifact endpoint, origin, security settings and `SOVITE_DEPLOYMENT_BINDING` as the installation. The Helm review records the latter value. Configuration changes invalidate old evidence rather than inheriting a green status.

Operators can inhibit a grant immediately from the console. This blocks dispatch and content release and requests cancellation of active work. Resume clears the runtime inhibit only when the current desired grant and qualification still authorize it. A blocked or cancelled task can retain an artifact for operator investigation. Reference contracts expose only fixed receipt/status projections; class-aware contracts and exact `result_readers` permit content only while the output class and current producing-job authority allow it. MCP never returns an artifact-store URL or provider task ID as a read credential.

Choose job and workflow input classes deliberately: omission selects the highest installed class, so lower-class public model routes require an explicit compatible input. Restricted inference remains private even if a cloud destination advertises a high clearance. Install public subscription/Go routes only with exact provider identities, credential references and qualified egress; see [native provider configuration](API_CONTRACT.md#native-authentication-and-provider-selection). A fixed native service must load the same bundle and, when used, the same `SOVITE_HOSTING_ENVELOPE` as the platform so both bind the identical configuration revision. The service-only hosted profile continues to permit internal model-gateway connections only.

For a class reduction or private-to-public workflow transfer, an authorized approver must first perform the installed output contract's procedure and record approval of the exact source digest and output bytes with pinned evidence references. Transfer requests name that durable release and the bound transfer; they cannot append new task text. Current source authority, output contract, receiving role and destination are rechecked at use. An output class label or a completed model call does not constitute declassification approval.

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

Database schema versions move forward through ordered migrations. Schema version 2 adds `binding_runtime_state` for model-binding inhibits; 0.1.9 and earlier ignore that table, so rolling an upgraded database back to 0.1.9 is safe. A release refuses to start on a schema version it does not know. A backup restored into an older release must not be newer than that release's schema.

## Database migrations and hosted startup

Stage the database explicitly before starting a hosted instance. Set `SOVITE_DATABASE_URL` to the migration role's connection URL and, for PostgreSQL isolation, set `SOVITE_DATABASE_SCHEMA` to its pre-provisioned schema. Run:

```sh
sovite db migrate
sovite db check
```

These commands use only database settings; they need no operator token, bundle, artifact credentials or signing keys and do not activate a generation. `migrate` creates the version 1 tables and applies the ordered binding-state (2), execution-authority/helper enrollment (3), setup-identity/draft (4), and worker execution lease/operation journal (5) steps, recording each version in `platform_metadata`. Existing data and audit history survive the upgrade; repeated migration checks compatibility without resetting state. SQLite DDL and version writes share an explicit transaction; PostgreSQL uses transactional DDL. Run one migration process at a time with all control and worker processes stopped, after a verified backup. There is no automatic downgrade.

Switch the runtime URL to the runtime role, keep the same schema, and set `SOVITE_DATABASE_MIGRATE=never`. Run `sovite db check` as that role, then start the API and workers. `Platform.initialize()` checks compatibility before bundle activation or work; it issues no DDL in this mode. Existing first-bundle activation and operational DML still occur after a successful check. The separate native harness receives no platform database credentials and owns an independent local SQLite execution ledger; these settings govern the platform database only.

`db check` is read-only and exits 0 with `{"status":"compatible","schema_version":5}` on success. It exits 1 with JSON containing a stable `code` on stderr for compatibility failures. Startup raises the same code before dispatch:

### Upgrade from 0.1.15 / schema 3 to 0.1.21 / schema 5

Schema 5 is not backward-readable by 0.1.15 (schema 3) or 0.1.17 (schema 4).
This is a one-way database upgrade; the source has no downgrade migration. For
the migration window, stop the control API and every local or remote worker,
take and verify a database and artifact backup, then run the candidate 0.1.21
image once with the migration-role `SOVITE_DATABASE_URL` and the existing
`SOVITE_DATABASE_SCHEMA` value:

```sh
sovite db migrate
sovite db check
```

Require the check to report `schema_version: 5`. Then configure the runtime
role, retain the same schema name, set `SOVITE_DATABASE_MIGRATE=never`, and run
`sovite db check` again before starting 0.1.21 control and local worker
processes. Use a separately provisioned execution-token map if remote workers
will be enabled. Do not let an older image start against schema 5; rollback to
0.1.15 or 0.1.17 requires restoring the verified pre-migration database and
matching artifact backup, then starting the corresponding release. The new
worker lease/operation tables are additive, but the old code rejects the newer
schema marker before doing work.

| Code | Meaning |
| --- | --- |
| `database_schema_missing` | No Sovite schema has been migrated, or the configured PostgreSQL namespace is absent. |
| `database_schema_older` | A supported older version needs `db migrate`. |
| `database_schema_newer` | The stored version is newer than this release supports. Use a compatible release. |
| `database_schema_unknown` | The version marker is malformed or unsupported. |
| `database_schema_incomplete` | Required tables, columns, types, nullability, primary/unique keys, indexes or version/audit-head metadata are missing or incompatible. Restore a supported schema and investigate. |

Migration also refuses unknown/newer versions and incomplete existing schemas before issuing DDL. It does not infer a version from partial tables or recreate missing audit metadata. `db check` does not create a missing SQLite database file or change its journal mode. Compatibility checks validate storage structure and required metadata; use `sovite audit verify` separately to verify audit history.

| Setting | Default | Behavior |
| --- | --- | --- |
| `SOVITE_DATABASE_URL` | `sqlite:///.state/control.db` | Migration or runtime connection URL, selected for the command/process. |
| `SOVITE_DATABASE_SCHEMA` | Unset (empty environment value also means unset) | PostgreSQL schema for every Sovite table; unset preserves the connection's existing default schema behavior. SQLite rejects an explicitly configured schema. |
| `SOVITE_DATABASE_MIGRATE` | `startup` | `startup` runs migrations during initialization for existing deployments; `never` performs compatibility checking only. Explicit `db migrate` still migrates when this is `never`. Other values are rejected. |

Helm values `database.schema: ""` and `database.migrate: startup` map to the schema/migration settings on control and worker pods. Compose accepts the same environment variables with those defaults. For hosted operation set `database.schema` and `database.migrate: never`, and deliver only runtime credentials to workloads. Run migration with a separately supplied migration URL before reconciling workloads. This lifecycle does not provision roles or add a chart migration Job.

Configured schema names must match `[a-z][a-z0-9_]{0,62}`; `public`, `information_schema` and `pg_*` names are refused. Provision the schema before migration. SQLAlchemy's `schema_translate_map` qualifies every ORM and migration table reference with that schema, and inspection specifies it explicitly. This avoids `search_path` fallback to `public` or another schema when the configured namespace/table is absent. Database grants enforce access boundaries independently; schema qualification alone cannot constrain a role with broad grants. The explicit schema also participates in the runtime qualification fingerprint; changing it invalidates existing evidence. Unset schemas retain the existing fingerprint.

Provision dedicated roles inside the workspace database. The migration role owns only the Sovite schema and tables; the runtime role has `CONNECT`, schema `USAGE`, and table `SELECT`, `INSERT`, `UPDATE`, `DELETE`, with no ownership, schema `CREATE`, database `CREATE`, role inheritance or membership in the migration role. Neither role may access work/audit schemas. For example, as the database administrator (substitute database/schema/role names):

```sql
CREATE ROLE sovite_migration LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE sovite_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE workspace_database TO sovite_migration, sovite_runtime;
CREATE SCHEMA workspace_sovite AUTHORIZATION sovite_migration;
REVOKE ALL ON SCHEMA workspace_sovite FROM PUBLIC;
GRANT USAGE ON SCHEMA workspace_sovite TO sovite_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sovite_migration IN SCHEMA workspace_sovite
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sovite_runtime;
```

After migration, apply the equivalent grant to any previously created tables:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA workspace_sovite TO sovite_runtime;
```

No sequence grants are currently needed: Sovite assigns audit sequence numbers itself. Remove inherited `PUBLIC` access to other workspace schemas and any `PUBLIC` schema/database `CREATE` grants through the workspace database's provisioning policy. A direct role revoke cannot cancel a grant inherited from `PUBLIC` or another role. Verify effective privileges, denied table creation and denied reads of other schemas before enabling dispatch. Supply authentication through the deployment's secret system; do not reuse an owner/superuser URL at runtime.

PostgreSQL migration/isolation tests use `SOVITE_TEST_POSTGRES_URL` pointing to a disposable superuser-accessible database. They create uniquely named schemas and non-login migration/runtime roles, use `SET ROLE` for each role's connections, and drop the fixtures afterward. SQLite tests run without that variable; PostgreSQL tests skip explicitly when it is unset.

## Failure handling and removal

Model outages leave authorized jobs queued or retrying according to their grant. They do not invoke an undeclared cloud fallback. Durable leases let a worker recover abandoned work; non-idempotent or uncertain execution requires intervention instead of blindly repeating an action. A worker that is stopped mid-task (rollout, node drain, eviction) hands the task back at once instead of leaving its lease to expire: a replay-safe task returns to the queue as `worker_shutdown` without being charged the interrupted attempt, and a task that is not replay-safe, such as native Codex, is blocked as `execution_uncertain`. Before 0.1.7 a stopped worker recorded its running task as cancelled. A harness or model that disconnects mid-response, for example because its pod was killed, is treated as an outage: a bounded model call retries, and a non-idempotent harness call such as native Codex is blocked as `execution_uncertain`. Before 0.1.8 that disconnect surfaced as `failed`/`internal_execution_error`, and in 0.1.8 as `failed`/`external_execution_uncertain`; from 0.1.9 every route to an unknown external effect ends `blocked`/`execution_uncertain`. The native service stores request identity separately from the worker queue and returns a cached completed result for a repeat of the same request.

`health/live` checks the API process; `health/ready` checks database and artifact access. Neither endpoint certifies model quality, GPU placement or information-flow policy. `/metrics` is unauthenticated Prometheus text for in-cluster scraping. It reports numbers only: whether qualification is required, whether the active generation is qualified, the expiry of its latest trusted qualification (`sovite_qualification_expiry_timestamp_seconds`, 0 when none) and the healthy worker count. Alert well before expiry: an expired qualification refuses new work and blocks queued tasks at dispatch. Allow scrapers with `networkPolicy.metricsScrapers` and keep `/metrics` off public routes. Use the console's worker, queue, inhibit, qualification and audit views to distinguish readiness from authorization.

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

## Isolated model gateway (PR 3)

Status: **an opt-in P08 remote-worker candidate routes tool-agent Chat requests
through the inference listener; default and native workers remain unrouted**.
Do not enable it as an enforced route. The local candidate has no chart or
worker egress policy, and does not qualify retry accounting, enrollment
issuance or demand cutover.

Run `sovite db migrate` with the migration role before upgrading runtime processes.
Schema version 3 adds `execution_authority`, `helper_enrollments`, `helper_sessions`
and `inference_requests`; versions 1 and 2 upgrade without altering job snapshots,
qualification, inhibits or the audit chain. Schema version 3 is not readable by
releases before this one; rollback requires a database restore from before the upgrade.
Runtime
startup can use `SOVITE_DATABASE_MIGRATE=never` and `sovite db check` as before.

The control service needs `SOVITE_AUTHORITY_TOKEN`, a dedicated random credential
of at least 24 characters, and `SOVITE_EXECUTION_WORKLOAD_IDENTITY` (initially
`worker`). It accepts only that gateway credential on `/internal/authority/*`.
Keep those routes private and serve control through authenticated HTTPS trust.
Operator/caller tokens and helper certificates cannot call them.

Start the separate process with `sovite gateway`. It reads only the JSON file
named by `SOVITE_GATEWAY_SETTINGS`. Its allowed fields are:

| Field | Purpose |
|---|---|
| `authority_url`, `authority_ca_file` | Fixed HTTPS control origin and explicit workload trust bundle |
| `authority_token` | Gateway's dedicated authority credential |
| `workload_tokens` | Map of execution workload IDs to distinct inference bearer credentials |
| `cert_file`, `key_file`, `helper_ca_file` | Gateway server certificate/key and separate helper client trust |
| `connector_host`, `connector_port` | mTLS WebSocket listener, defaults `0.0.0.0:8443` |
| `inference_host`, `inference_port` | Workload HTTPS listener, defaults `127.0.0.1:8444` |

Mount a reduced environment and only those secrets. **Never mount the shared
runtime environment**, database, S3, operator credentials or CA signing keys in
this process. Helper trust must use a separate CA/intermediate from workload
trust. Both gateway listeners require TLS 1.3; the connector requires a client
certificate and the `sovite-inference.v1` subprotocol. Compression, ambient proxy
use, payload access logging and redirects are disabled.

Preprovision approved helper certificates out of band, then register their public
certificate with `sovite.authority.provision_enrollment()` against the active
binding, installation, enrollment ID and connector role. This administrative
in-process registry writes public trust only; certificate issuance, renewal and
rotation automation arrive in PR 6. Do not reuse a helper key as workload identity.

`Gateway.infer()` is the internal async entrypoint. The HTTPS equivalent is
`POST /inference/{binding_id}/{operation}` with the exact inference body and:

- `Authorization: Bearer <execution workload credential>`
- `X-Sovite-Capability`, `X-Sovite-Job`, `X-Sovite-Fence`
- `X-Sovite-Deadline` (absolute whole Unix seconds)

Successful streaming preserves the original chunks/SSE bytes. A terminal gateway
failure after response headers is an SSE `sovite_error` event containing only a
bounded `code`; it must be treated as failure. Pre-stream denials return JSON.
These staging interfaces do not configure native provider clients (PR 4).

Operator-only `POST /api/bindings/{id}/inhibit` and `/resume` accept the same
`{"reason":"..."}` body as grant controls. Inhibit fences helper sessions. Resume
requires an enabled binding and current qualification. After
`model_cancel_unconfirmed`, independently establish that old generation has
stopped before explicitly clearing the inhibit and reconnecting a helper.
A gateway restart produces visible interruption, never recovery by replay.

Worker lease renewal refreshes the current execution capability to at most 60
seconds from renewal, capped by the execution deadline, in the same locked
transaction. Revoked capabilities and replaced fences are never revived. Permits expire
within three seconds, control calls within one second, and cancellation waits at
most the binding's qualified bound capped at six seconds. A failed control call
never extends a permit. The development pilot proves local HTTP operation stop
against a cooperative test engine; it does not prove real provider stop bounds.
Qualification must account for permit expiry, engine abort latency and transport
latency together. Public test evidence contains no installation addresses or data.

Terminal inference requests and closed helper sessions are retained for
`SOVITE_AUTHORITY_RETENTION_DAYS` (positive integer, default 7). Worker lease
recovery, including idle polling, sweeps at most once per hour across all workers,
deleting at most 500 rows from each table per sweep. Request age is measured from
the terminal update; session age from closure. Active rows and sessions referenced
by retained requests are preserved. This sweep does not delete job or audit history.
Abandoned active requests whose permit plus the binding's cancellation bound has
elapsed become `interrupted` / `model_cancel_unconfirmed` at the next authority
check and visibly inhibit the binding. Reconnect closes stale helper sessions
after six seconds without a control heartbeat, or when they belong to an old
gateway; uncertain requests still require the operator inhibit/resume procedure.

## Hosted mode: policy roles and offline qualification

The hosted policy contract is in [PLATFORM-SPEC](PLATFORM-SPEC.md#hosted-policy-envelope-and-qualification).
This is a service-only policy/qualification foundation, not a hosted Helm profile.
Do not copy the standalone chart's direct `modelPeers`, provider egress or
`modelCredentials` into hosted deployment configuration. Preserve strict default
deny, including DNS policy, and qualify the service-only network explicitly.
Required internal paths include DNS and PostgreSQL, as well as approved control,
gateway and authority services. No public model/provider Internet egress is granted
by an envelope; helpers initiate connections. Actual network rendering and packet
qualification belong to the hosted deployment work.

Supply a host-controlled `SOVITE_HOSTING_ENVELOPE` JSON file and separate tokens
of at least 24 characters in `SOVITE_AUTHOR_TOKEN`, `SOVITE_ACTIVATOR_TOKEN` and
`SOVITE_OPERATOR_TOKEN`. All role and caller tokens must differ. Keep
`SOVITE_REQUIRE_QUALIFICATION=true`. The envelope selects the workspace,
installation, revision, namespace, total resource ceilings, classes, exact service
destinations (including complete retention declarations) and approved packages; its schema and bounds are public owner
contracts. A minimal placement policy is:

```json
{"namespace":"workspace","resources":{"max_replicas":1,"max_cpu_millis":1000,"max_memory_bytes":536870912}}
```

This is the `placement` field, not a complete envelope. Fill every required field
from `config/hosting.schema.json`; do not accept tenant-provided envelope files.
Bundle namespaces must equal the host selection; approved hosts use
`service.namespace.svc` or `service.namespace.svc.cluster.local`. C1 must be
explicitly present in `allowed_data_classes`. Canonical C2/C3 vocabulary can remain
in the bundle, but grants, destinations and hosted content cannot use those classes.

After explicit database migration, `sovite init` leaves a new hosted installation
awaiting activation. Authors submit `{"bundle": ...}` to `POST /api/bundles` or
run `sovite compile path/to/bundle.yaml` with `SOVITE_AUTHOR_TOKEN`. An activator
calls `POST /api/bundles/{generation}/activate` or runs
`sovite activate GENERATION` with `SOVITE_ACTIVATOR_TOKEN`. Both hosted CLI
commands are authenticated clients of the running control service: they send one
bounded HTTPS request, need no database credential, and never read
`SOVITE_HOSTING_ENVELOPE`, whose authority stays with the serving process.

Point them at the service explicitly with `SOVITE_CONTROL_URL`: a fixed HTTPS
origin with an optional port and no credentials, path, query or fragment. Set
`SOVITE_CONTROL_CA_FILE` only to add a private CA; ordinary certificate and
hostname validation stays enabled and system trust is used otherwise. The bundle
path is the positional argument, else `SOVITE_BUNDLE_PATH`, else
`config/platform.local.yaml`. Compilation requires an HTTP 201 response and
activation 200. Other statuses, redirects, malformed, encoded or oversized
responses, TLS/transport failures and missing configuration exit 1 with bounded
diagnostics that never include the selected token or response text. There are no
automatic retries. If an activation request loses transport, the outcome is
unknown: inspect the current server state before retrying, because the server may
already have activated that generation. Do not assume rollback. A server denial
remains a denial.

Hosted mode refuses `reload` before any runtime or database work; compilation and
activation must be separate steps. The API server requires all role tokens. A
hosted CLI command loads only its role's configured token, so neither command
needs the other role's token or the operator token. Other online administrative
CLI commands use `SOVITE_OPERATOR_TOKEN`. An author has no activation or
operational artifact access. The operator can inhibit/cancel and inspect private
operational state, but its token cannot author or activate hosted policy. Bearer
credentials and cookie sessions follow the existing auth scheme; cookie mutations
require the exact configured Origin. Database owners and direct database access
remain trusted and can rewrite configuration and authority; this client boundary
does not by itself split database roles or isolate those owners. Keep database
credentials on trusted administrative hosts and use the HTTP API for tenant role
enforcement. Standalone configuration changes remain Git/CLI only; bundle
mutation HTTP routes do not exist.

Every destination's `retention` object, including its in-memory cache bounds and
cleanup flags, must exactly match the host-owned envelope, even for unused and
non-inference destinations. Tenants cannot enable logging or persistent context
beyond that declaration.

Provision public trust with `SOVITE_QUALIFICATION_ISSUERS`, a JSON array such as:

```json
[
  {"path":"/trust/platform.pem","role":"platform","not_before":1700000000,"expires_at":1893456000},
  {"path":"/trust/tenant.pem","role":"tenant","workspace":"workspace-a","expires_at":1893456000}
]
```

Times are Unix seconds; choose actual reviewed validity windows. Both current
verification time and the record's signed `created_at` must fall in the issuer's
`not_before <= time < expires_at` interval. Explicit issuers
require `path`, `role`, `expires_at`; `not_before` is optional. Only tenant issuers
carry `workspace`. Each key must have exactly one issuer entry; duplicate key
material, even in different files or roles, is a startup configuration error. `operator` is
supported for standalone trust; the existing `SOVITE_QUALIFICATION_PUBLIC_KEYS`
list remains valid outside hosted mode. Unscoped keys do not count in hosted mode.

Keep the tenant Ed25519 private key on an owner-controlled signing device or
workstation and the separate platform key outside the hosted cluster. Neither
belongs on the helper, worker or API, and neither is a release-promotion key.
Runtime services need public trust and signed records only. Collect deployment
evidence for each role using the exact check names in PLATFORM-SPEC. Record actual
measurements, limitations and current runtime binding; synthetic or not-run
results cannot qualify hosted dispatch.

On the signing workstation, provide the reviewed bundle and envelope and the
installation's **nonsecret** runtime settings (database identity/schema, artifact
placement, origin, secure-cookie/qualification settings and deployment binding).
Database passwords and runtime bearer tokens are unnecessary in hosted offline
signing. `keys`, `runtime-binding` and offline `qualify --output` do not open a
database or construct a runtime. Standalone `keys` and `runtime-binding` retain
their operator-token requirement. Enforced bundles require evidence with the
exact runtime binding for both online and offline qualification.
Select the relevant private key only on that workstation:

```bash
sovite validate path/to/bundle.yaml
sovite runtime-binding
SOVITE_QUALIFICATION_PRIVATE_KEY=/offline/tenant-private.pem sovite qualify --role tenant --evidence evidence/tenant.json --profile hosted --issuer workspace-owner --output tenant.qualification.json
SOVITE_QUALIFICATION_PRIVATE_KEY=/offline/platform-private.pem sovite qualify --role platform --evidence evidence/platform.json --profile hosted --issuer hosting-platform --output platform.qualification.json
```

Set `SOVITE_BUNDLE_PATH` and `SOVITE_HOSTING_ENVELOPE` to the reviewed local files;
`--profile` must match the bundle's effective profile and evidence. The examples
assume a profile named `hosted`. Import on the runtime's administrative host:

```bash
sovite qualification-import tenant.qualification.json
sovite qualification-import platform.qualification.json
sovite qualification-export QUALIFICATION_ID --output archived.qualification.json
```

Import verifies the signature, key id, role, workspace, generation/profile, runtime
binding, envelope revision, validity and required checks before storing. Duplicate
ids, other-workspace records and superseded generations are refused. Export
includes the payload, signature, PEM public key and `public_key_id`, the SHA-256
digest of the raw 32-byte Ed25519 public key. Never include a private key. Retain
these files with the evidence set in the owner's approved storage.

Revoke by adding key ids to `SOVITE_QUALIFICATION_REVOKED_KEYS`, a JSON array of
`sha256:...` values, or remove their issuer entries. Deploy the updated trust
configuration to **every** API and worker process. Environment settings load at
process start: restart/reconcile those processes to apply a changed list. Once
applied, every authorization check refuses the revoked key immediately, including
claim, renewal and release; historical signed records remain stored for audit.
Key expiry applies independently of record expiry. Missing/unreadable trust files
fail closed. For rotation, use distinct keys and entries, obtain both newly valid
role records, and remove/revoke superseded keys deliberately.

Any envelope change, including placement or revision, changes hosted generation
identity and the runtime binding. Recompile, activate and obtain both signatures
against that new binding. Existing inhibits remain; after restore, run the existing
recovery lock and review effects before resuming. A valid signature pair is no
substitute for actual gateway, external-worker, artifact-custody and network
qualification; those integrations are not supplied by this policy change.


### Bounded agent-led setup (opt-in source capability)

Schema version 4 adds `setup_principals` and `setup_drafts`. Use the existing explicit
`db migrate` procedure before starting with `SOVITE_DATABASE_MIGRATE=never`. Older
binaries refuse the newer schema; restore a compatible backup for rollback. Runtime
setup requests perform DML only. Back up both tables together with bundle/audit state.

Enable with `SOVITE_SETUP_API_ENABLED=true` and an explicit, stable
`SOVITE_SETUP_INSTALLATION_ID`. Changing that ID makes previous setup credentials and
drafts unusable. Enablement and the active setup installation ID are included in
the runtime qualification binding; changing them requires fresh qualification.
Defaults leave all setup endpoints absent. This local identity is
not a hosted tenant assertion, workload attestation or general task identity.

1. Operator: `POST /api/setup/principals` with `name`, optional `ttl_seconds` (60–86400),
   and `envelope`: exact new `models` definitions, existing `harnesses`, existing
   `grants`, and `max_steps`. The token appears once in this response; store it on
   the authorized agent side. The database stores only its SHA-256 digest.
2. Setup agent: `GET /api/setup/context`, then `POST /api/setup/drafts` with
   `idempotency_key`, `base_generation`, one exact approved `model`, `agent_id`,
   `agent_name`, `harness`, `instructions`, `max_steps`, and `grant`. New agents have
   no tools. Only the selected existing grant's agent reference changes. No caller,
   clearance, storage, credentials, compute or runtime settings can be edited.
3. Agent: `GET /api/setup/my-drafts/{id}` returns its own draft metadata. Agent
   responses never contain the installation bundle. Operator:
   `GET /api/setup/drafts/{id}` inspects the full validated proposed bundle.
4. Operator: `POST /api/setup/drafts/{id}/activate` with the exact `generation`.
   Expired/revoked author or changed active base refuses activation. An identical
   activation retry succeeds only while that revision is still active and its
   author remains current. Qualification is never minted by setup or activation;
   apply the existing qualification procedure to the exact new generation.
5. Operator: `GET /api/setup/principals` inventories credential IDs without tokens
   or token digests (100 per page; pass `after=next_cursor` for the next page). Use
   this to revoke unused principals after an uncertain mint response; a lost token
   cannot be recovered. Operator: `POST /api/setup/principals/{id}/revoke` immediately prevents further
   proposals and activation of pending drafts. Revocation does not revert an already
   activated configuration; inhibit affected grants using existing operator controls.

Use ordinary task-caller and worker identities for execution. Setup credentials
cannot use operator, task, artifact or qualification surfaces. See
[the implementation plan](AGENTIC-SETUP-PLAN.md) for remaining real-model, Vuoro and
hosted acceptance gates. The setup API is not enabled in hosted deployment packaging.


For a disposable real-inference assessment, run
`python scripts/qualify_agentic_setup.py --help`. The runner requires an explicit
loopback Ollama endpoint and expected digest; it refuses non-C0/non-synthetic inputs,
never downloads a model or substitutes a synthetic adapter, and writes private bytes
only to the explicitly supplied `--private-artifact` path. The summary is bounded
execution evidence; it does not accept Vuoro work or qualify hosted operation.
