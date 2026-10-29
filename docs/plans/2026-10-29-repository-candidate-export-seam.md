# Repository candidate export source seam

Status: **uninstalled source, not a deployed or qualified service**. This
bounded owner helper has no HTTP route, no new artifact authority and no
production executor. The caller must already be authenticated by the enclosing
owner and be an approver under a current existing Sovite output contract. That
contract must have approved the exact canonical UTF-8 export content for the
completed source job and requested destination. A bare work lease, candidate SHA,
receipt URI, operator-supplied path or this helper does not grant disclosure.

The protected-side `_prepare_for_owner_review` is an internal owner operation,
not a caller read endpoint. It assembles bytes for the existing output-approval
procedure. `export` rechecks current job, grant, binding, execution profile and
output-release authority, and compares the exact approved bytes before return.
It accepts only owner run/attempt identities, output release and destination;
all filesystem locations are derived from the admitted owner record. A successful
read records a bounded audit event. This source seam requires the existing
execution authority to remain current, including its deadline; exporting after
that deadline is currently unavailable, not an implicit continuation grant.

The bounded JSON content carries base/tip commit identities, receipt and patch
hashes, and a base64 Git bundle with an explicit base prerequisite. A temporary
private Git repository names the retained candidate commit directly and includes
no unrelated refs. Candidate history must be linear from that base. The owner
validates the retained immutable receipt, its exact stored digest and execution
packet/fence binding, and a clean worktree snapshot before and after bundle
construction. Internal fence values and filesystem paths are not included in the
export envelope. Candidate bytes can themselves contain sensitive content and
remain subject to the installed output contract and its review procedure.

The envelope is limited to the smaller of the installed request limit and 8 MiB.
The current output-release API handles UTF-8 text only, so large or binary exports
without this bounded representation fail closed. There is no installed protected
approval workflow, candidate-export route, cross-host retention guarantee,
current Sprintctl lease recheck or real executor qualification here. A reader
must obtain the exact base separately and independently verify its objects. A
later integration must add qualified authentication, source continuity, work
owner currentness and authorized delivery; this helper never settles work or
proves a candidate is correct.
