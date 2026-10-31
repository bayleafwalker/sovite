# Native protocol VM fixture

`native_vm_protocol.py` runs the actual published `repository_native_runtime.py`
on a disposable supervisor peer, over routine SSH into a different KVM guest.
It needs Python 3.12, websockets, and the pinned Codex 0.166 vendor ELF. It uses a
declared exception-type import shim, a synthetic HTTP model, and an explicitly
unqualified authorization stub. It has no provider credential or paid model
operation and cannot issue an execution profile or qualification.

The Cloud-owned fixture seeds the guest first. Before creating the seed bundle,
write the map returned by `--print-seed-files` into the prepared seed. These are
candidate attempts to introduce local MCP, hooks and feature changes; they must
not gain supervisor placement. The supervisor home `/tmp/native-vm-supervisor`
must be absent before the probe. The guest cwd defaults to `/workspace/repository`.

Supply `--runtime-module`, `--codex`, and `--ssh-argv-json`. The last file contains
the fixture's fixed routine SSH argv, without a trailing verb; the probe appends
only `exec-v1`. It must target the routine forced-command lane with its distinct
fixture key. The production callback still uses its own closed Cloud descriptor
and pinned known-host/routine-key checks. Test argv is not an installed runtime
configuration surface.

Eight synthetic model requests exercise shell placement and credential/sentinel
exclusion, interactive stdin, file image RPC, actual unsupported builtin
`apply_patch`, bounded user-input refusal and uncommitted guest edits with actual
Git metadata write refusal. The Cloud exporter must retain those edits using its
bounded nonroot scratch snapshot after quiescence; the agent cannot write `.git`.
The fixture deliberately bypasses paid inference validation to measure native
`apply_patch` refusal and image transport. The production inference bridge rejects
these unsupported response/input paths before native disclosure or a further
paid call, respectively; its actual HTTP/currentness/accounting controls have
separate tests.

Success prints runtime/ELF hashes, native version and method inventory, without
candidate output, credentials or raw model context. The Cloud owner must then
independently quiesce, validate the exact acknowledgment and export/verify the
real candidate bundle. A successful native transport probe does not qualify
provider inventory, lease/grant currentness, model fees or the complete service.
