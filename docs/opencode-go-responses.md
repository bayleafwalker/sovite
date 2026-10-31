# Public Go Responses source

This source extends the existing `opencode-go` adapter and `provider-service`
identity. It adds no provider registry, model router or local weight assertion.
The native Codex loop remains on the protected supervisor, with tools in the
disposable guest and model credentials outside that guest. Ordinary direct CLI
execution refuses this Go path: only the installed repository runtime supplies
the actual lease, current authorization and per-call budget.

No real Go profile is installed or qualified by this change. The provider's
actual extra-usage setting, hard billable input bound and current account quota
must be proved before existing deployment qualification can enable it. Numbers
in a configuration file do not prove those facts. In particular, native client
catalog defaults are not provider limits. OpenAI publishes a 1,050,000-token
context and 128,000-token output limit for
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna); applying
that OEM limit to the Go proxy's complete billing behavior still needs owner
review. This source makes no such inference automatically.

The endpoint is the existing fixed Go service, with `wire_api: responses` and
either its base URL or exact `/responses` URL. The transport normalizes to the
fixed endpoint, sends the actual Job ID as `x-opencode-session`, and uses a
stable Sovite user agent. It preserves the existing bounded stream, whole
deadline, stateless body validation and fresh authority checks. OpenCode
documents [native Responses support for Codex](https://opencode.ai/v2/docs/console/harnesses/codex/).
No capability or provider key is added to candidate inputs.

Go Responses requires enforced v1alpha2 and an explicit public destination.
Its clearance must be literal `C0` at rank zero, with `trust_tier: provider`.
The existing destination retention declaration must include `max_seconds:
2592000` and an owner-published `authorization_ref`, allow prompt/response
logging, refuse persistent response context, and describe provider-managed
caching without claiming local cancellation or session-close erasure. The
30-day upper bound reflects the published
[GPT-6 Luna Go retention terms](https://opencode.ai/v2/docs/console/go/#privacy).
This is permission for public/synthetic content, not a zero-retention claim.
C1-C3 input, output and source/tool joins remain excluded. Gateway and public
hosting-envelope restrictions remain unchanged; v1alpha1 is not an escape.

Existing accounting fields reserve a positive worst-case charge before each
request, including possible unqualified balance fallback. Go adds one closed
`opencode_go` object to the protected accounting configuration:

- `billing_source`: the proved `go` or `go-plus` account mode.
- `reservation_allowance`: the proved worst-case allowance for one request.
- `maximum_root_allowance`: the owner-selected cap for the original run and all repairs.
- `maximum_allowance_per_list_usd`: the proved upper ratio of allowance units to list dollars.

The existing token limits and rates must cover the actual hard billable input
ceiling, output cap, long-context prices and every applicable cache-write price.
The allowance reservation must cover that whole bound too. There are no guessed
rate, quota, credit or context defaults. The existing qualification binds the
protected configuration and current policy generation; no extra manual signing
or approval mechanism is introduced.

[OpenCode's API accounting](https://opencode.ai/v2/docs/console/api/inference/)
distinguishes actual balance charge (`usage.cost`), list price
(`opencode.list_cost`) and Go allowance (`opencode.allowance`). The free Go path
settles zero dollars only when a complete response explicitly reports zero,
the expected Go billing source, nonestimated list price and known allowance.
Balance/BYOK fallback, positive charge, estimated or missing metrics, stream
loss and overrun stop the attempt while retaining reservations and slots.
Subscription price is not silently assigned a zero task cost.

Money and allowance observations preserve decimal values from the original
response. A tiny positive wire charge cannot underflow into a known zero.
Unrepresentable, nonfinite or excessive-precision amounts remain unavailable.
The existing Job reservation records retain actual charge, list price and
allowance with distinct units. Root allowance checks run under the existing
owner transaction lock across the original Job and its admitted repair chain.
Children cannot raise or reset that cap, and unknown legacy quota observations
cannot become zero. This is a per-root cap, not a second account-balance ledger;
Go remains the owner of its account-wide period limits.

Tests use synthetic Responses traffic, real budget persistence and actual
Workspace/repair admission on SQLite and PostgreSQL. They qualify source
behavior only, without contacting a paid model or asserting account capacity.

The pinned native 0.166.0 catalog normally selects Responses Lite and code mode
for GPT-6 Luna. This runtime uses the supported startup `model_catalog_json`
setting with one fixed bundled catalog for the same actual model ID. It
preserves the upstream native instructions and narrows tool presentation to
the four admitted standard functions. Code mode, native agents, tool search,
apps, hooks and plugins are disabled; neither model routing nor billing limits
come from this catalog. The request inventory is checked before any paid call.

The bundled catalog derives from OpenAI Codex
[`rust-v0.166.0/models-manager/models.json`](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/models-manager/models.json)
(upstream SHA256 `1384625c0b6b3e67d3c60da29becea2b017f2c283fc1e2d05b8edafb4698eb10`).
Only GPT-6 Luna is retained. The changed fields are `shell_type`, `tool_mode`,
`multi_agent_version`, `apply_patch_tool_type`, `experimental_supported_tools`,
`support_verbosity`, `use_responses_lite`, `supports_search_tool`, and the three
`include_*_usage_instructions` fields. The accompanying upstream Apache 2.0
license is packaged with the asset. No caller can supply a catalog path.
