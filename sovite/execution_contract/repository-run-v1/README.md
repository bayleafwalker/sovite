# Repository run v1

`contract.json` defines the submission envelope and the nested Vuoro
`portable-work/v1` packet. The packet is intent, not execution authority. The
owner resolves repository access, executor publication, allowed areas, base
revision, deadline, and mutable-profile qualification through its trusted
binding resolver. It must reject a packet that does not match that binding.

## Caller identity and preview binding

`preview_id` is SHA-256 over canonical JSON containing the exact packet and the
authenticated principal. The principal comes from the Sovite-authenticated
request context; it must never be copied from a packet field or trusted from a
caller-supplied identity header.

An eventual Vuoro-to-Sovite adapter must preserve the identity that Vuoro used
to create its preview. A shared service credential cannot simply forward a
browser user ID as if Sovite had authenticated that user. Before enabling that
adapter, the two owners need a qualified delegation or per-principal binding
that lets Sovite verify the caller-to-workspace/repository authority chain.

## Mutable executor qualification

The profile is disabled unless an owner explicitly composes a qualified
mutable executor. This package does not provide a native CLI or provider
executor, and its test stub is not a production qualification. Qualification
must cover process isolation and repository confinement, deadline and cancel
behavior, crash/recovery behavior, and the provider usage boundary.

The packet's `max_cost_usd` and `concurrency` are admitted against owner
published limits. Runtime enforcement is cooperative: an executor must acquire
a concurrency slot and reserve estimated cost before every paid call, then
settle the actual charge even when it exceeds its reservation. Overages must be
recorded and stop further work. A provider integration that cannot reserve
before calls and reconcile actual usage does not enforce a hard spend ceiling
and must not claim one or enable the mutable profile.

### Trusted mutable execution profile

A composition must supply a callable returning the current closed
`sovite-repository-execution-profile/v1` record to both the owner and attempt
executor. It must bind the exact executor ID, publication revision,
qualification reference, digest, and expiry selected by the repository
resolver. The mode must be `mutable-native`, and these claims must all be
true: per-attempt isolation, credential containment, exact binding, durable
checkpoint and candidate, predecessor reconciliation, and bounded usage.
Missing, malformed, unsupported, expired, or mismatched records deny discovery
readiness and admission. The worker reads the current profile again when it
checks attempt authorization; revocation after an effect begins makes the
attempt uncertain and prevents release. A callable that fails is unavailable.
No production profile or execution host is supplied here.

These fields are claims made by the trusted commissioning owner; this code
validates their presence and binding, not the physical isolation, actual native
lifecycle, spending controls, or the authenticity of qualification evidence.
An installation must establish those properties independently. The profile is
internal and does not change the versioned caller capability response shape.

When an executor has several repository bindings, an available discovery
entry lists only repositories whose current bindings match the installed
profile; its publication revision and conservative limits come from those same
bindings. If none match, discovery reports the executor unavailable and retains
the resolved repository IDs for diagnostics. A specific admission still
resolves and checks its own binding. An issued attempt packet must retain the
admitted binding ID and declared packet version before workspace preparation.
A binding whose owner deadline has passed is unavailable. Discovery compares
and orders timezone-aware deadlines as instants, so differing UTC offsets do not
change which deadline is earliest. Invalid or timezone-naive owner deadlines
fail closed, and ongoing authorization stops at the admitted run deadline.
