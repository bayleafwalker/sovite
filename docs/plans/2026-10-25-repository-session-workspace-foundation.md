# Decision record: repository-session workspace foundation

Status: **internal foundation only; no repository-session owner API or launch
capability is released.** This decision applies to the implementation at the
current source revision. It does not qualify a host, repository, model, or
provider.

## Decision

Add a closed internal packet and local Git workspace/evidence utility in
`sovite/repository_sessions.py`. The utility accepts only the versioned
`sovite-repository-session/v1` packet and resolves its `binding_id` through an
injected trusted owner interface. The owner binding supplies the canonical
local bare mirror root and all packet authority fields. Packet data cannot
select a path, command, URL, credential, provider, or execution host.

An attempt gets one deterministic private worktree keyed by workspace,
repository, job, binding, and attempt identity. It is detached at the exact
full base commit. An identical recovery returns that worktree without reset or
cleanup; a changed packet under the same attempt identity is rejected. The
utility records immutable evidence containing base and candidate tip,
binary-patch digest, changed paths, deadline status, cleanliness, and any
scope violations. Added and deleted paths are represented independently by
disabling rename detection. Untracked, ignored, out-of-scope, and symlink paths
make evidence ineligible for publication.

The utility is not connected to job admission, `LeaseService`, the worker,
provider launchers, or `agent-http-v1`. It creates a Git worktree and candidate
evidence only. It does not claim process isolation, provider-session recovery,
or merge/publication authority. The repo-session owner API remains unreleased
until its owner contract, execution adapter, enrolled-host capability,
recovery behavior, qualification, and integration tests are implemented and
reviewed separately.

## Packet fields

The closed packet currently binds:

- `binding_id`, `job_id`, `workspace_id`, and `repository_id`;
- a full lowercase Git commit SHA in `base_sha`;
- normalized relative `allowed_areas`;
- timezone-aware ISO `deadline`;
- `attempt_id`, `generation`, and `fence` from the current owner context.
- `allowed_areas` contains at most 256 entries; each path is at most 4,096
  characters and the total UTF-8 path bytes are capped at 64 KiB.

Unknown fields fail validation. In particular, a packet cannot contain a local
mirror path, repository URL, shell command, credential payload, or provider
configuration. `RepositorySessionManager` requires a trusted resolver exposing
`resolve_repository_binding(binding_id)` or a trusted callable with the same
behavior. That resolver is an in-process integration seam, not a network
endpoint or an authorization implementation.

## Recovery and evidence

Durable state is private to the configured state directory. Creation intent is
written before `git worktree add`; after a process interruption, the same
attempt can finish recording that exact worktree. Candidate evidence is written
to a private temporary file, flushed, then atomically renamed under the
per-attempt lock before its state pointer is updated. A crash during the write
leaves no visible receipt; a later capture removes that attempt's stale
temporary file and completes a fresh receipt. Recovery verifies and adopts the
same published receipt instead of generating a replacement. The lock protects
cooperating writers in this utility; it is not a same-user hostile-writer
boundary. Git stdout is capped while being read, and candidate patch bytes are
hashed as a bounded stream. A prepared worktree moved away
from its base without candidate evidence is rejected. A candidate changed
after receipt creation is rejected, while the original receipt remains.

Evidence may be captured after its deadline so that the result is not lost;
such evidence records `within_deadline: false` and cannot be marked
`publication_eligible`. Scope diagnostics are evidence, not an enforcement
sandbox. No process is launched by this module, and no API currently consumes
these receipts.

## Validation

`tests/test_repository_sessions.py` exercises closed packet fields, owner
binding mismatch, exact-base worktree creation, restart and two crash windows,
reused attempt identity, main and sibling repository non-mutation, add/rename/
delete path evidence, untracked and out-of-scope paths, symlink diagnostics,
candidate immutability, partial temporary-receipt writes, bounded/slow Git
subprocess output, and expired evidence handling.
