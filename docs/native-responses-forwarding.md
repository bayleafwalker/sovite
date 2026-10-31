# Native Responses forwarding prerequisite

`ModelGatewayInference.responses(model, payload, authorization_check)` forwards
one stateless native request through the existing fixed model gateway binding.
It uses the current owner job, lease fence, deadline and execution capability;
request content cannot select an endpoint, model binding, workload or credential.
This is a client prerequisite, not an installed native runtime or a new router.
The existing helper remains development-only and does not become qualified by
accepting this wire shape.

The client accepts the text/function Responses subset and requires `stream:true`
and `store:false`. Only bridge-local `client_metadata` and `prompt_cache_key`
are removed. Other unknown fields fail closed. An optional `include` must be
exactly `["reasoning.encrypted_content"]`. Reasoning history uses a closed opaque
encrypted item with text summaries; absent, null or empty `content` is permitted
for native serialization, and plaintext reasoning content is rejected. Hosted
and custom tools, images, compaction items and caller-selected URLs remain
unsupported. Function declarations and results are inert inference data; the
native runtime retains its tool loop.

A successful `ResponsesResult` contains:

| Field | Meaning |
| --- | --- |
| `sse` | Exact buffered stream bytes, at most 4 MiB |
| `response` | The terminal `response.completed` response object |
| `usage` | Valid observed token counters, or `None` when usage is unknown |

The client requires one complete terminal event, checks the exact returned
model and validates nonnegative usage counters when present. Stream loss or
missing completion is an uncertain outcome. It never retries a request. A
whole-operation deadline bounds connection and every stream read; incoming
keepalive chunks do not extend it. Current authorization is checked for each
received block and immediately before disclosing any buffered bytes. Revoked,
expired, failed, oversized or incomplete streams are not returned. Error
messages never copy upstream messages or bearer material.

The protected caller must reserve cost before invoking this method. Token
usage is not a billed dollar amount. Apply the installed accounting policy
only to trustworthy completed observations; retain reservations and reconcile
unknown usage or effects. Missing completion or `usage:null` never means zero
cost. This source does not implement the paid-provider native bridge or budget
settlement.

The contract follows OpenAI's [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)
and [stateless encrypted reasoning continuity](https://developers.openai.com/api/docs/guides/reasoning).
The accepted subset is deliberately narrower than the full Responses API.

`tests/test_native_responses.py` exercises real loopback HTTP stream loss,
trickled-byte deadlines, revocation, malformed/unknown usage, bounded output,
and strict request refusal. Installed binary compatibility is opt-in:

```sh
SOVITE_CODEX_COMPAT_EXECUTABLE="$(command -v codex)" \
  python -m pytest tests/test_native_responses.py
```

The compatibility check requires `codex-cli 0.166.0`, disables hosted web search
and request/stream retries, uses a clean native home without provider credentials,
and drives an actual function-tool turn against a fixed synthetic endpoint.
It verifies the changed file and encrypted reasoning/function-result replay.
This establishes native wire compatibility only: no paid inference, real model
competence, VM containment, production cancellation or service commissioning
is established by that test.
