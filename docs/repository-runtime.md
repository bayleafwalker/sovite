# Installed repository execution composition

`SOVITE_REPOSITORY_RUNTIME_CONFIG` opts the API and database-backed worker into
one protected host composition. Without it, or if it cannot be composed,
repository launch remains unavailable. Other worker profiles continue normally.
The API's embedded worker uses the same lifecycle executor as its repository
owner and candidate exporter; a standalone `sovite worker` builds the same
composition from its installed configuration.

This source slice does not ship `sovite.repository_native_vm`. Configuring a
runtime therefore remains unavailable until that fixed builtin implementation
and its qualification are installed. The actual provider VM lifecycle, native
process boundary, and credential-contained native model bridge remain separate
implementation and commissioning work. The existing model helper is explicitly
development-only; this composition does not promote it.

The file is host-owner configuration, not a grant database or an API request.
The file and every parent must have canonical paths without symbolic links.
The file must be a private regular file owned by root or the service UID, with
one hard link and a protected parent. Install updates by atomic replacement,
retaining those ownership and permission constraints. Candidate code must run
outside this host UID and must not receive these files, the protected state,
credentials, or control-plane access.

The closed top-level JSON object has these five required fields and one optional repair policy:

| Field | Meaning |
| --- | --- |
| `schema_version` | `sovite-repository-runtime/v1` |
| `state_root` | Absolute private directory for protected repository records and receipts |
| `bindings` | Nonempty array of exact `RepositoryRunBinding` objects from `sovite/repository_runs.py` |
| `driver` | Fixed builtin `sovite.repository_native_vm.create_driver` configuration |
| `retention` | `null`, or the fixed Workspace transport configuration below |
| `repair` (optional) | Absent/null keeps repair disabled; otherwise the exact bounded policy below |

There is no configurable module import or executable plugin. If the builtin
VM driver is not installed, the composition stays unavailable. The driver owns
its `execution_profile()` provider. Setting profile booleans in the installed
JSON does not qualify a host and is rejected. A composed runtime still needs
the driver's current qualification to match the binding before launch.

Each binding narrows an existing platform grant for one exact
`(workspace_id, repository_id, principal_id)` tuple. Binding IDs and tuples must
be unique. Allowed areas must be valid session paths in sorted order without
duplicates. Every resolver call rereads the protected file and rechecks the
current platform grant, caller, input class, generation, operational inhibition,
and platform qualification. An execution packet is reconstructed from the
exact admitted job and current started lease, fence, attempt, generation, and
deadline.

The optional driver `for_attempt(context)` hook receives a frozen, protected
`RepositoryAttemptContext` constructed from the actual started owner lease.
It includes detached copies of the current model and, for model-gateway jobs,
the validated transient execution capability and owner context. Its deadline
cannot exceed the started lease deadline. These values stay on the protected
supervisor; they are never guest or tool-process input. Ordinary five-argument
executor callbacks retain their existing interface.

No lookup selects a binding's newest attempt. Candidate export binds
its repository manager to the exact durable packet of the requested candidate,
so a concurrent sibling attempt cannot replace it.

Binding edits take effect at the next owner check. Changing the driver,
protected state path, retention configuration, or repair policy disables an existing resolver
until the process is recomposed. Enabling or changing the runtime configuration
path also changes the platform deployment fingerprint and requires matching
platform qualification. Driver qualification separately covers the actual VM,
credential and network boundaries; source configuration is not that evidence.

For retention, install the pinned `retention` extra and use this closed shape:

```json
{
  "root": "/var/lib/sovite/repository-publications",
  "profile_path": "/etc/sovite/workspace-profile.json",
  "credential_file": "/run/credentials/sovite/workspace-token",
  "target": {
    "endpoint": "https://workspace.example.invalid",
    "workspace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "destination": "workspace-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "allowed_classes": ["C3"]
  }
}
```

The profile is a real `vuoro-client-profile/v1` document validated by the
released Vuoro client. Its endpoint must match the fixed target, and its
`credential_ref` must be `file:` followed by the exact absolute
`credential_file`. Profile and credential files have the same private-file
constraints as the runtime configuration. The resolver rereads credential
bytes on each use; a changed profile requires recomposition. Request bodies
cannot select a Workspace endpoint, credential file, destination, clearance,
or output approval procedure. The current output contract and Workspace
membership still authorize each transfer.

For repair, the same protected file may contain:

```json
"repair": {"max_repairs": 1, "feedback_contract_ids": ["qualified-feedback"]}
```

`max_repairs` is1–3; contract IDs select existing current output-owner procedures.
They do not create approval authority. A non-null policy requires the same fixed
retention reader and a driver declaring support for separate `repair_context`.
Unsupported drivers, missing readers and malformed requested policies disable
the optional composition. The ordinary five-field configuration keeps repair
disabled. Configuration alone proves no native or feedback qualification.

The [generated repair contract1.1](contracts/repository-repair-v1.md) adds the
read-only `repair-feedback-context` operation. Only a caller that owns the parent
and is an actual enabled approver for the installed feedback procedure can read
its exact source/contract/generation/candidate selectors. The read creates no
approval; the existing output-release API remains the authority for a qualified
machine or other already-authorized procedure. Candidate model disclosure is
checked separately from Workspace readership. Unknown approval/repair replies
must reconcile the same saved bytes and keys, preserving current principal and
source authority.

The composition does not add a mutable profile to the remote execution API,
whose admitted external recipe remains `agent-http-v1`. It does not turn the
existing answer-only native harness into a mutable runtime or commission a
provider VM. The installed native VM driver, real failure-case qualification,
and deployment evidence are separate requirements.
