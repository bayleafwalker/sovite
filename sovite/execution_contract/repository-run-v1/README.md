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
