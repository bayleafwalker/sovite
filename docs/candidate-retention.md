# Retained repository candidates

The opt-in `CandidatePublicationService` composes the actual candidate export
owner with a fixed Workspace target and the released Vuoro client. It does not
install a mutable executor, grant a work lease or accept a repository result.
The API remains unavailable unless the real repository-run owner and matching
publication service are explicitly composed.

A configured caller may POST `/api/repository-runs/v1/runs/{run_id}/candidate-retention`
with its `workspace_id`, completed `attempt_id` and exact existing `release_id`.
The host selects the private endpoint, Workspace membership credential, allowed
classes and destination; callers cannot select any of them in the request.
The installed output contract must already approve `workspace-<Workspace ULID>`
and name the target hostname in that destination's `allowed_hosts`. Normal
output-contract approver and exact-byte checks still apply. This adds no new
approval procedure or universal confirmation step.

## Protocol

Publication files live in a private protected host directory outside disposable
candidate workers. They contain exact released UTF-8 export JSON, its full
SHA256 and size, disclosure provenance, the fixed destination, actual Workspace
principal, exact normalized begin request and stable idempotency key. Files are
fsynced before the first write request. A nonblocking flock coordinates one host;
it grants no cross-host fencing or execution authority.

| Persisted phase | Recovery |
| --- | --- |
| prepared / begin-unknown | Recheck current output authority and current Workspace membership, then replay the same principal-guarded begin/key. A changed principal stops before allocation; profile filenames supply no identity proof. |
| reserved | Recheck current output authority and fixed host/class/destination before uploading exact saved bytes. |
| upload-unknown / finalize-unknown | Retry finalize for the exact known version before any upload. Only the real owner's `knowledge-object-missing` refusal permits a new upload, which requires fresh output authority and the reserving principal. |
| ready | Reconcile the same version and append an exact locator to the actual repository run's evidence. Repeated acknowledgement converges without duplicating evidence. |

An expired producer grant cannot authorize a new upload. Already uploaded bytes
can be finalized by a current authorized Workspace contributor without reviving
that grant. A ready candidate can be downloaded by a fresh contributor after
producer expiry or worker removal, under Workspace's current membership rules.
A mere reader still needs the normal independent Workspace approval. Membership
is not model clearance: current reader class/destination policy is checked
before downloading. A trusted owner-anchored receipt identifies the exact
artifact/version/digest; feed/latest lookups never resolve ambiguous begin.

The retained JSON preserves inner Git bundle and fence-free receipt digests and
adds the whole-export digest and size. The complete JSON, not merely the decoded
bundle, is bounded to 8 MiB so it fits the served Workspace transfer envelope.
Git remains repository authority; Workspace is retained transport/evidence.
The immutable output release is unchanged; publication state is not grafted
onto resource-gateway or work-authority records.

## Evidence and limits

Actual integration tests use Sovite's real Git/export/SQLite owner, the released
Workspace adapter in the real Vuoro HTTP shell, disposable PostgreSQL and the
filesystem object store. They inject response loss after owner commit for
begin/upload/finalize, verify one version and one evidence locator, remove worker
state and replace producer execution authority, then exercise fresh contributor,
reader-only, removed-member and foreign-caller access. They also test changed
storage/read clearance, corruption, disabled API composition, and current output
revocation before new writes. These are bounded source/integration proofs, not
an installed operated service, multi-host publication, restore or second-tenant
qualification. Host publication files need the host owner's backup/restore;
owner records carry durable version locators and membership remains current.
