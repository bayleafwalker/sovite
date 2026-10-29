# Resource-owner read seam

Status: **internal source seam, uninstalled and unqualified**. No public API,
worker route, live gateway, resource-operation journal or deployment is added.
This does not establish an authorized resource connection, model disclosure,
local cluster access, audit retention or a qualified customer capability.

`ResourceGatewayRead` accepts only workspace, resource-domain and catalog
observation selectors. An independently authenticated composition supplies
tenant, principal, attempt and fence context; selectors do not authenticate.
A trusted local resolver binds those identities to one named pod, a fixed
namespace and an existing `kubernetes-read` tool configured for `list-pods`.
The injected owner checks current attempt and resource authority before the
read and before disclosure. The owner independently decides whether the exact
canonical projected bytes and digest are releasable under the bound current
output-contract revision and destination. Both decisions require literal
approval from owner-provided callbacks; model output is not an authority.

The existing adapter owns its own service credential and bounded HTTP read;
its raw response stays on the resource side. The seam emits only the selected
pod identity and a known phase. Missing, duplicate, wrong-namespace and
unsupported target data fail closed, and upstream errors are not echoed.
The exact binding is re-resolved after the target read and after the output
decision; authorization is also rechecked before return. This check does not
replace a real service's synchronized authority, isolation, currentness,
receipt persistence or output-release protocol. Existing Sovite artifact
output releases apply to completed jobs and are not silently treated as this
new read's approval. A production composer must supply and qualify the catalog,
authenticated context, authorization and output-decision providers on the
resource-owner side, as well as a compatible enclosing operation protocol.
