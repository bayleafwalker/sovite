# Upstreams and package boundaries

Sovite Platform composes a serving system, model binding, agent package, native harness, compute placement, private state and a flow contract. The distribution does not implement a new tensor server or replace the Codex agent loop. The following versions were checked against primary project sources on 19 October 2026. A checked version is a packaging input, not proof that a particular cluster has been provisioned or qualified.

| Component | Shipped binding | Upstream source | Assessment boundary |
|---|---|---|---|
| Ollama | `ollama/ollama:0.40.6`; local inference, pre-staged model volume, cloud features disabled | [Release](https://github.com/ollama/ollama/releases/tag/v0.40.6), [container guide](https://docs.ollama.com/docker), [local-only setting](https://docs.ollama.com/faq) | Model digest and hardware identity require observation on the target. A server being healthy says nothing about task competence. |
| Codex CLI | `@openai/codex@0.163.0` in the optional native image; isolated HTTP service runs the native process | [Release](https://github.com/openai/codex/releases/tag/rust-v0.163.0), [source](https://github.com/openai/codex) | Native sandbox support, model compatibility and tools are qualified as a concrete tuple. The core worker image has no Codex process or shared native filesystem. |
| PostgreSQL | `postgres:17.11-alpine`; durable job and policy state | [Official image](https://hub.docker.com/_/postgres) | Bundled installation has one database instance. A production HA database can be supplied through the same database URL binding. |
| Garage | `dxflrs/garage:v2.4.1`; S3-compatible private objects | [Quick start](https://garagehq.deuxfleurs.fr/documentation/quick-start/), [configuration](https://garagehq.deuxfleurs.fr/documentation/reference-manual/configuration/), [cluster deployment](https://garagehq.deuxfleurs.fr/documentation/cookbook/real-world/) | Bundled installation uses one node with fsync enabled. It supplies persistence, not node redundancy. Use a separately operated redundant store when the availability requirement needs it. |
| Kubernetes NetworkPolicy | Explicit ingress/egress between named component roles | [Kubernetes documentation](https://kubernetes.io/docs/concepts/services-networking/network-policies/) | Enforcement depends on the target CNI. Rendering a policy is not observation that the policy is active. |
| Cilium DNS policy | Exact approved service-name queries, alongside default-deny network rules | [Cilium DNS documentation](https://docs.cilium.io/en/stable/security/dns/) | Arbitrary DNS is not opened for workers or native agents. External state/tool names and addresses must be declared explicitly. |
| Helm | API v2 chart, verified locally with Helm 4.3.1 | [Release and checksums](https://github.com/helm/helm/releases/tag/v4.3.1) | Chart validation checks rendering, schema and selected structural invariants. It cannot prove installation, driver support or runtime confinement. |
| Docker Compose | Specification parsed with the official Compose 5.6.0 CLI | [Release and binary artifacts](https://github.com/docker/compose/releases/tag/v5.6.0) | Base, native and GPU configurations were merged and validated without a Docker daemon. Container images and host networking were not executed by this check. |
| Flux | Standard `HelmRelease` example with a Git source and values projection | [Flux HelmRelease documentation](https://fluxcd.io/flux/components/helm/helmreleases/) | The example must be attached to an actual repository, trust source and encrypted secret delivery. No repository or cluster is assumed to exist. |

## Pinning and updates

Development profiles carry explicit version tags for upstream components. Enforced profiles require SHA-256 image references and a resolved model identity. `scripts/lock_images.py` pulls named published images, reads the registry references returned by Docker, and writes a new lock file. It never substitutes a made-up digest and never publishes an image. Python dependencies are recorded in `requirements.lock`; image base references can be supplied through build arguments and locked by the build pipeline.

Any model, tool, harness, grant or bundle change creates a new desired generation. Current deployment settings are part of the qualification binding. A prior signature cannot authorize a changed tuple. Runtime inhibition remains separate from configuration and must survive attempts to roll back authority through an older desired revision.

The Helm installer binds its reviewed rendered resources, chart hashes, complete public values and bundle/trust inputs into `SOVITE_DEPLOYMENT_BINDING`. This records desired deployment identity and is incorporated into runtime qualification. It does not substitute for observing pod image identities, policy enforcement or the target model's capabilities. The exact local checks and the verified Helm/Compose binary hashes are in `DEPLOYMENT-VALIDATION.json`.

## Optional integrations

The implemented serving adapters and HTTP harness boundary are extension points. KServe, vLLM, kagent, ToolHive and other controllers can be adopted behind those contracts, but they are not claimed as installed adapters in this release merely because their names appeared in the earlier architecture. Add a concrete profile, identity observation, network binding and qualification evidence when introducing one. This keeps the complete installed platform assessable: every displayed component has a corresponding runtime, configuration or explicitly unprovisioned binding.

## Name

**Sovite Platform** is the provisional product name. It emphasizes fitting independently managed parts together and covers serving, agents, hosts and their operating contracts. The previous Tulli name emphasized controlled crossing of boundaries. Brief public repository checks found adjacent Docker tooling already named Telakka and Laituri, and an active Wi-Fi software company named Sidos. Exact or similarly spelled Sovite repositories also exist. The choice is an architectural working name, not a trademark, domain or package-namespace clearance.
