# Closed resource-owner targets v2

Status: source only. No resource policy, credential, migration, gateway deployment,
health observation or KVM qualification is implied by this change. The v1 pod
protocol remains available with its original named-pod meaning. Its historical
source description is [resource-gateway-service](../plans/2026-10-29-resource-gateway-service.md).

The owning wire source is [main.tsp](../../contracts/resource-owner-v2/main.tsp),
compiled by the frozen Vuoro generator recorded in
[provenance](resource-owner-v2.provenance.json). The generated SDK and schema
bundle are included in the Sovite package. Python 3.12 and the pinned released
`vuoro-contracts` runtime are required for v2; v1 has no new runtime requirement.
There are seven fixed internal operations for scope installation, output grant,
submission, poll, currentness, result and exact result read. Existing operator,
workload and enrolled gateway authentication owns those distinct actions.
The bounded Authorization header describes transport, not authority delegation.

A workload scope contains one or two entries, at most one per kind, with unique
logical target IDs. The closed operations are `model-peer.health-read`,
`kvm-lab.create`, `kvm-lab.observe` and `kvm-lab.stop`. A request cannot name a
URL, host path, unit, image, command or credential. It includes the actual
started execution's job, instance, generation, fence and stable operation ID.
The owner derives its request ID from the actual job and operation, and rechecks
current workload credentials, lease, execution authority and installed project
currentness before admission, effects and new disclosure. A Work currentness
snapshot is a condition; it supplies no effect grant or Work acceptance.

Every entry has resource-owner-declared source/output classes and destination.
V2 supports explicit C0-to-C0, C1-to-C1 and C2-to-C2 profiles. No class is inferred
from the health target or public image; a C0 demonstration requires actual owner
policy explicitly classifying the bounded projection C0. Job input and grant
clearance, current model/execution destination boundaries, and exact active
output grant still apply. C3 and declassification profiles are unsupported in
this slice. Existing pod C1/C2-to-C1 behavior is unchanged. Output grants bind
workload, principal, gateway, tenant, workspace, domain, target, operation-specific
schema, destination and policy revision. Stored output is disclosed only after
fresh exact owner checks.

## Installed target configuration

The new gateway has a protected `sovite-resource-installed-catalog/v2` JSON file.
Entries have these closed shapes:

```json
{"kind":"model-peer-health","target_id":"peer-daemon","origin":"https://192.168.10.55:8021","method":"GET","path":"/healthz","ca_file":"/absolute/public-ca.pem","ca_sha256":"<64 lowercase hex>"}
```

```json
{"kind":"kvm-lab","target_id":"diskless-lab","template_id":"diskless-kvm-lab-v1","template_sha256":"<64 lowercase hex>","max_ttl_seconds":300}
```

The target revision is `sha256:` plus SHA256 of the entry's canonical sorted,
compact, ASCII JSON. Public configuration and CA files must be regular,
non-symlink files owned by root or the trusted gateway UID, with no unrelated
writer in the path. Public read permissions are allowed. Credential files use
the existing stricter secret-file reader. Configuration/CA/token changes fail
closed and require deliberate process restart and current owner policy.

The health executor uses only the selected fixed HTTPS IP, port and GET path,
with the exact pinned public CA, no bearer, proxy environment, query or redirect.
It requires status 200 and exactly `{"error":{"message":"ok"}}`. Its projection
means only that the peer HTTP daemon responded. It makes no upstream-model,
inference, readiness or catalogue claim. Response bytes and whole HTTP duration
are bounded; raw peer data is never released.

The lab executor invokes only `/run/current-system/sw/bin/cloud-kvm-lab` with no
arguments, one canonical JSON line and EOF. The bundle contains closed
`LabHostRequest` and `LabHostObservation` schemas for the fixed local Unix-helper
protocol. Create uses its actual owner request ID as create ID. Observe/stop
name an exact retained create belonging to the same caller/tenant/domain and
unchanged installed target configuration. Current policy and a new exact output
grant authorize each follow-up; the old policy grants no continuing authority.
The original create deadline is retained exactly and cannot be extended by a
new policy or follow-up.

The separate root helper must enforce the fixed public diskless template,
512 MiB/one vCPU, cap one, TTL at most 300 seconds, no network/disks/shared paths,
no guest input or credentials, and boot/unit/InvocationID reconciliation. A
missing future create is UNKNOWN; stop must tombstone a missing create so a late
queued create cannot start. Root-owned systemd TTL independently bounds cleanup.
That host implementation and actual KVM qualification are separate prerequisites.
The public projection omits unit, boot IDs, InvocationID and raw guest output.
`effect_outcome: unknown` is explicit and never settles execution cost, completes
the Job, accepts Work or authorizes candidate publication.

## Installation and unknown outcomes

The gateway VM hosts only the local policy, existing local operation store and
outbound poller. Execution-owner rows stay in the external control plane. There
is no admin HTTP listener or admin token on this VM. The fixed entry point is:

```
sovite-resource-gateway-v2 --database /private/operations --catalog /protected/catalog.json install
sovite-resource-gateway-v2 --database /private/operations --catalog /protected/catalog.json catalog-check
sovite-resource-gateway-v2 --database /private/operations --catalog /protected/catalog.json policy-install
sovite-resource-gateway-v2 --database /private/operations --catalog /protected/catalog.json poll
```

Install the unchanged local store schema1 explicitly as its unprivileged owner;
poll never runs DDL. `catalog-check` emits only IDs, kinds and revisions.
Root SSH administration supplies one bounded closed `GatewayPolicy` JSON on
stdin to `policy-install`. It validates policy/configuration, checks the existing
private regular database and inode, forks, clears environment/supplementary
groups and drops to that database's UID/GID before calling the existing policy
store. Root credentials are never passed to the poller. Unit tests cover that
ordering with mocked privilege syscalls; actual root-UID host qualification is
not claimed by those tests.

Poll scans at most 100 existing requests per page, advancing past fenced rows.
Poll requires only `SOVITE_RESOURCE_OWNER_ORIGIN`, public
`SOVITE_RESOURCE_OWNER_CA`, and `SOVITE_RESOURCE_GATEWAY_TOKEN_FILE` (for an
explicitly installed limited enrollment credential). There is no ambient bearer
value fallback. No default resource policy or image-embedded credential exists.
The poller emits fixed codes/counts, never raw target data or exception text.

The existing local operation table durably admits the exact owner packet,
policy digest and fixed effect descriptor before effects. Immutable observation
bytes and release metadata precede delivery. A lost delivery reply replays only
those retained bytes after current checks. An admission without a receipt after
restart remains UNKNOWN and never reissues create. A new separately authorized
observe/stop can reconcile the exact retained create descriptor. An unknown
helper response becomes an explicit unknown projection; it never creates a
second VM. This local history is operational state, not permission, Work,
execution or publication authority, and is not a distributed fencing guarantee.

Each remote call and local helper exchange has a five-second total budget and
bounded bytes. Local database serialization can add its existing bounded wait.
Authorization is checked before each effect and after calls before disclosure;
there is no atomic cross-network side-effect guarantee, zero-latency revocation,
or promise to undo an already admitted call. Root TTL remains independent of
polling, Work lifetime and control-plane connectivity.

## External owner schema7

`sovite db migrate` is an explicit offline source migration. Startup checks
compatibility only. Frozen schema6 definitions preserve all four existing
resource tables. Schema7 changes only the existing scope/request tables: a
closed catalogue JSON on scope; nullable legacy observation/namespace/pod for a
catalogue scope; nullable paired namespace/pod for typed requests. Each request
still has a meaningful actual target in `observation_id`. Exclusive shape
checks prevent mixing catalogue and legacy pod selectors.

PostgreSQL takes bounded table locks and transactional ALTERs. SQLite rebuilds
only these two tables inside the existing explicit transaction. Exact preflight
refuses foreign columns, indexes, triggers, constraints or incoming references,
rather than discarding extensions. Existing observation bytes, grants, tokens,
IDs and pod selectors are preserved. Faulted migration rolls back both table
changes and version stamp. Version6 refuses to start version7; version7 refuses
an older database until explicitly migrated. No live migration or release is
part of this source change.
