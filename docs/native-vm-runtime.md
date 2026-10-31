# Native VM runtime source

This is source for a protected Codex 0.166.0 loop with native tools in a separate
VM. It is **not an installed or qualified execution profile**. The fixed factory
uses only Cloud’s fixed `ProtectedInventoryObserver` reader of fresh protected
inventory. That collector/profile/inventory is not installed or qualified by
this source. Neither a descriptor nor
configuration booleans can make it available. Automatic per-attempt provider
allocation/reclamation remains Cloud work; this slice can consume only one
precommissioned, single-use descriptor for the exact started attempt.

The supervisor retains model credentials, the current execution capability,
lease/fence, live authorization callback, and durable budget. A fresh native
app-server runs in a protected local home and cwd. Its external execution
environment uses an authenticated loopback WebSocket relay carrying the native
JSON-RPC protocol over one SSH stdio channel. The guest receives no provider,
Sovite, forge, or operator credential. Routine SSH uses a separate observed key
and four fixed forced-command verbs; administrative SSH and TCP forwarding are
not the execution transport.

Before `thread/start`, `config/read` must show no MCP servers or hooks, with the
measured 0.166 feature flags for hooks, apps, plugins, remote plugins, goals and
native subagents disabled; see the upstream [hook configuration](https://learn.chatgpt.com/docs/hooks). Every first/subsequent inference request must expose
exactly `exec_command`, `write_stdin`, `request_user_input`, and `view_image`.
Unexpected tool inventory stops before reservation or model disclosure. Buffered
provider events and final output are also checked before native disclosure; an
unadvertised `apply_patch` or custom tool cannot hide in an intermediate event.
The pinned native inventory does not enable builtin `apply_patch` (the actual
custom-tool probe is refused without filesystem effects). Candidate
project/global config cannot become supervisor process placement. Files and
shell run through the external environment; missing/disconnected environments
never select a local fallback. Image input is still refused by the inference
validator; the file-read compatibility test does not enable paid image inference.

The guest needs the exact 0.166 vendor ELF and full qualified Nix closure. A
root-owned immutable `codex-linux-sandbox` argv0 helper pointing at that same ELF,
plus bubblewrap and ripgrep, must be in the fixed guest PATH. The ELF checksum
pins the protocol implementation; it does not attest the wrapper, dependency
closure, helper placement, guest process controls, or deployment. These need
separate measured deployment evidence.

Every native RPC request and response disclosure checks current owner authority;
a passive watchdog also closes on revocation/deadline. HTTP inference uses the
existing owner-published model recipe and destination, either the existing
gateway or its existing direct Responses adapter. The development-only gateway
helper cannot be promoted by configuration. Before each provider call, the
bridge acquires a durable slot and reserves the full proved worst-case token
cost. Accounting requires positive current rates, an actual provider/model
maximum billable input-token bound, output cap and reservation covering both.
A request byte cap is not an input-token estimate. Unknown usage, cost, stream
loss or cancellation retains the reservation/slot and forbids replay/new calls.
Subscription fees remain unsupported rather than being settled as zero.
The separate [public Go Responses source](opencode-go-responses.md) accepts
explicit known zero balance charge while tracking list price and root-scoped
allowance independently. It remains unavailable for real qualification until
the service/account bounds are proved; it does not extend C1-C3 privacy claims.

Cloud owns the installed `vuoro_cloud.native_vm_descriptor` validator, actual
independent resource observation, and guest verb implementation. Sovite supplies
the exact job, attempt, workspace, repository, generation, fence hash and deadline
from the started lease. Every authority check reloads the exact descriptor and
compares the complete newly observed resource, including boot and keys; an old
observation is not retained as authority. A differing callback deadline is refused before effects:
shortening only a local timeout cannot shorten an immutable root guest manifest
under partition. Guest root must enforce that deadline in its own cgroup,
persist a single-use start latch across reboot, and stop all candidate children
on channel loss. The routine lane must not inherit an unrestricted operator key.

After all native channels close, Sovite requires Cloud's authenticated quiescence
acknowledgment for the exact observed VM/boot/deployment/base. This cleanup also
runs after uncertain seed/start outcomes and after authorization revocation.
Export requires the identical previously validated acknowledgment, then a fresh
current owner check. An unknown outcome remains unresolved; it is not retried.
Interrupted partial bytes use the separate
[exact-attempt evidence recovery](repository-interruption-recovery.md) path.
It uses Cloud's bounded retention and sealed read-only export, without restarting
native execution, settling unknown usage or marking the original Job successful.

Untrusted exported Git is parsed in fresh quarantine by a separate fixed Nix Git
and `prlimit` process with minimal environment, no hooks/pagers/network protocols,
fsck on transfers, bounded output and the whole owner deadline. Hard and soft
limits cover CPU (8 seconds), address space (512 MiB), individual files (80 MiB),
file descriptors (64), processes (64 under a dedicated supervisor UID), and core
dumps (zero). Packs stay packed. Changed expanded regular blobs total at most
8 MiB, and must remain within allowed repository areas and linear descendants
of the exact seeded tip. Materialization uses the fresh quarantine config, so
host local filter definitions cannot execute. The existing manager still owns
final candidate capture and publication. Same-UID malicious candidate execution
on the supervisor is outside this architecture and is forbidden.

Repair input is an explicit `repair_context` keyword passed as native untrusted
additional context. The goal stays byte-for-byte unchanged. The owner already
prepared the parent candidate seed on the original canonical base. The feedback
hash binds the whole owner feedback envelope, not just its text.

The closed installed driver config is `sovite-native-vm-driver/v1`, with
`executor_id`, `publication_revision`, `native_store_path`, `native_sha256`,
`state_root`, `descriptor_path`, `descriptor_owner_uid`,
`routine_credentials_root`, `qualification_id`, and `accounting`. No field names
an executable, module, observer URL or arbitrary command. The native store path
is the pinned Codex package, and its `libexec/codex/bin/codex` ELF is hashed.
Accounting fields are `model_id`, `valid_until`, `maximum_request_bytes`,
`maximum_input_tokens`, `maximum_output_tokens`, `reservation_usd`,
`input_usd_per_million`, and `output_usd_per_million`. Supplying these values is
not proof of provider limits or prices.

Availability additionally requires an existing trusted, current signed Sovite
deployment qualification. `native_vm` evidence binds source SHA256, config
SHA256, native package/ELF, exact Git/prlimit paths and digests, image SHA256,
exact VM ID, execution profile/deployment IDs, native closure/helper SHA256,
installed supervisor manifest SHA256 and the
`single-use-cloud-vm` scope. Required passing checks include existing isolation,
credential, exact binding, durable candidate, reconciliation and bounded usage
checks plus native RPC routing, supervisor OS containment, config isolation, VM process lifecycle and
quiescence/import. Existing qualification authoring/signature verification is
used unchanged. The fixed observer must independently measure the exact VM and
match image, native, closure, helper and derived deployment facts before the
profile is advertised and again for the attempt. No real deployment record is issued by this source or its tests.

Local synthetic tests exercise actual installed 0.166 native shell, interactive
stdin, file RPC, candidate MCP refusal, current authorization, real HTTP
streaming and budget behavior. Their process factory and shared-UID process
limit fixture are explicitly unqualified. Separate KVM/SSH routine-lane,
root-cgroup, disconnect/reboot, actual model/provider cost and currentness proofs
remain required before this profile can serve a real run. The current callback
checks Sovite grant/lease/resolver authority; the served Work-owner basis
currentness wrapper must also be composed before claiming that stronger scope.


The native supervisor always starts through the fixed Bubblewrap namespace;
missing immutable installation facts or a kernel/container namespace refusal
returns `native_supervisor_unavailable`. There is no raw process fallback. The
root-owned Nix-store manifest installed at
`/run/current-system/sw/share/sovite/native-supervisor.json` declares schema
`sovite-native-supervisor/v1`, exact `bubblewrap`/`python` paths and SHA256,
`tools`, sorted `store_paths`, and `child_sha256`. Only that required closure is
mounted read-only. The private PID, mount, network, IPC, UTS and user namespaces
contain only a fresh native home, private proc/dev/tmp and two fixed socket
inodes. Further user namespaces are disabled and capabilities are dropped.

The fixed stdlib helper forwards two private loopback ports to those socket
inodes. The owner side forwards only to the actual current inference bridge and
native executor relay, checking current authorization before every block and
before disclosure. Connections are bounded, the original attempt deadline
bounds the helper lifetime, and PID namespace teardown stops detached children.
No direct DB, S3, mirror, provider credential or arbitrary network proxy is
mounted or exposed. The existing attempt-scoped bridge token remains inside the
private native home/process environment; broad owner capabilities stay outside.

The portable `tests/probes/native_supervisor_containment.py` measures the real
native host-RPC privilege seam using synthetic parent config, mirror and proc
markers, a disposable nonbridge endpoint, and a finite detached child. This seam
measures process privileges; it does not assert that the four admitted model
tools expose host RPCs. Test-only immutable fixture paths never configure the
production loader. Guard-removal tests must expose the synthetic file, host proc
status and direct network endpoint when their corresponding mounts or network
boundary are weakened. No real credentials or paid model are used.

A workstation namespace pass does not qualify the installed owner container.
Qualification additionally requires the same image, dedicated UID 11001,
actual runtime seccomp profile and kernel namespace settings, actual native
function-loop compatibility, and the `native-supervisor-containment` check
bound to the installed manifest digest. A runtime that rejects user namespaces
remains unavailable. This source does not authorize privileged operation,
`CAP_SYS_ADMIN`, an unconfined seccomp fallback, or a deployment. Legacy native
adapters outside this repository VM composition are not covered by this claim.
