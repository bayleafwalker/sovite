# Connector protocol v1 (PR 2)

This implements transport-independent protocol and identity building blocks and a
**development-only Phase 1 pilot**. There is no production gateway service,
request authorization, execution ledger, worker/native routing or deployable
connector chart yet (PRs 3–5). `require_model_adapter()` continues to reject every
`model-gateway` binding. Nothing here makes that path enforced-capable.

The helper opens one outbound TLS 1.3 WebSocket with a client certificate and
subprotocol `sovite-inference.v1`. Compression, ambient proxies, redirects and
payload logging are disabled. There is no reconnect loop, fallback, resume,
transparent replay, inbound helper listener, forwarding operation or helper tool
execution. A gateway must map the authenticated certificate to its issuer's
installation/binding/enrollment/role record and supply that record to identity
verification. A self-reported assertion does not establish enrollment authority.
The future gateway owns single-session admission and fencing across connections;
the pure state machine owns one active request within a connection.

## Wire contract

Binary WebSocket messages contain canonical UTF-8 JSON objects. Every message
requires exactly `version: 1`, `session_id`, `seq` and `type`, plus the fields
below. Each sender starts at sequence 1 and increments by exactly one across all
message types. IDs are 1–256 ASCII letters/digits or `_.:-`. Timestamps are whole
Unix seconds. Unknown fields/types, wrong directions, gaps, replays, unsolicited
responses and a different session terminate the session. At most 1,024 requests
are accepted per session, bounding the duplicate-ID set; then reconnect with a
fresh session and challenge. Reconnection never reissues an interrupted request.

`context` below expands to `load_epoch`, `engine_process_instance`,
`host_instance_id`. No message accepts a URL, generic method or arbitrary headers.

| Direction | Type | Required fields beyond envelope |
|---|---|---|
| G → H | challenge | nonce, installation_id, binding_id, enrollment_id, enrollment_role |
| H → G | hello | identity (`assertion`, `signature`) |
| Both | heartbeat | ready (boolean), context |
| G → H | prepare | request_id, operation, body_digest, context |
| H → G | prepared | request_id, identity (request-scoped assertion/signature) |
| G → H | invoke | request_id, operation, body, deadline, permit_ttl_ms |
| H → G | accepted | request_id, context |
| H → G | chunk | request_id, data |
| H → G | complete | request_id, context |
| H → G | error | request_id, code |
| G → H | permit | request_id, permit_ttl_ms |
| G → H | cancel | request_id |
| H → G | cancelled | request_id, context |
| H → G | renew_csr | csr (at most 16 KiB; **schema only**, PR 6 implements issuance) |

`operation` is exactly `chat`, `responses` or `ollama-chat`. Each helper is
configured for one API, advertises exactly that singleton in `wire_apis`, and
rejects a different preparation operation with `invalid_request` before any
engine contact. The signed preparation operation must equal the binding
`wire_api` and the advertised API. `body` and `data` are
strict, padded standard base64 of opaque inference bytes; bodies are not converted
between APIs. Control messages are at most 64 KiB encoded, enforced before JSON decoding.
A capped canonical envelope detaches only the first `body` (invoke) or `data`
(chunk) base64 field; every remaining byte must fit the control bound, and the
message type must match. Invoke base64 decoding, digest verification and inference
JSON validation run once in a worker thread. The admitted header reserves the
request while heartbeats, watchdog and cancel remain responsive; no engine contact
or acceptance occurs until payload validation finishes. Chunks contain at most
64 KiB decoded, with an envelope bound of base64 length plus 1 KiB. Invoke has an
explicit decoded body bound (default and maximum 4 MiB), plus at most 64 KiB of
encoding overhead. Accumulated result bytes are also bounded by the body limit.
A single SSE event exceeding 64 KiB is refused rather than split. SSE delimiters
and event bytes are preserved; Ollama streaming emits one NDJSON line per chunk,
with a 64 KiB line bound including the newline (an unterminated final line is
preserved within the same bound). HTTP read-block boundaries do not define lines.

Errors are fixed private codes: `model_host_preempted`, `engine_unavailable`,
`invalid_request`, `deadline_exceeded`, `permit_expired`, `result_too_large`,
`engine_protocol_error`. Upstream error text and payloads are never copied into
error frames. These are connector errors, not public receipt statuses.

## State and verification

Both peers observe the same transitions:

`new → challenged → idle → preparing → prepared → invoked → streaming → idle`.

Only `prepare` creates a request. Only matching `prepared` allows `invoke`; the
operation and SHA-256 digest must match. Only `accepted` allows chunks/completion.
An error terminates an active request. Cancel transitions an active request to
`cancelling`; only `cancelled` acknowledges termination. A closed session cannot
be reused. During cancellation, already emitted responses are drained with the
same correlation, ordering and result bounds; they do not replace the stop
acknowledgement. A cancel crossing a completed response is acknowledged from a
single terminal-request record. Late permits for that terminal request are
ignored and cannot restart generation. Heartbeats and schema-only renewal do not
create inference authority.

The production gateway must perform, in order:

1. Authenticate the certificate against approved enrollment and call
   `verify_connection()` with the independently expected identity and fresh nonce.
2. Send `prepare` with the private digest, without prompt bytes. Call
   `verify_preparation()` before sending `invoke`. It checks the request, the
   complete connection assertion, v1alpha2 binding/model/role/destination/API,
   engine version, weight-manifest digest and both connector-role image digests.
3. Retain the returned verification as private request evidence. Call
   `verify_continuity()` through completion; the state machine independently
   rejects changed instance/epoch context. A replacement is `model_host_preempted`.

The pure state machine validates correlation and assertion consistency; it does
not authenticate signatures, decide current execution authority, or persist
private evidence. Calling it alone is insufficient to authorize prompt delivery.
`unpinned-development` is accepted only with `mode="development"`, and the
verification explicitly carries `pinning="unpinned-development"`. Enforced mode
also rejects `disconnect-only` cancellation. Other cancellation labels still
require independent qualification of the engine's reservation and stop guarantee.

## Canonical identity encoding

The custom encoder and recursive decoder accept only strings, signed integers
within `[-9007199254740991, 9007199254740991]`, booleans, arrays and objects. Null,
floats, exponent notation, negative zero, leading zeroes, duplicate keys and
noncanonical encodings are rejected. There is no whitespace or BOM; keys are
sorted by Unicode code point; strings use raw UTF-8 except quote/backslash escapes
and lowercase `\u00xx` for ASCII controls. Surrogates are rejected; Unicode is not
normalized. Strings are bounded to 64 KiB, containers to 1,024 entries, nesting to
24 levels. Signed assertions have tighter field limits and fit a control frame.
Only the opaque base64 inference payload uses the larger explicit string limit.

Ed25519 signs `b"sovite/helper-signed-v1\x00" + canonical(assertion)`. Signatures
are 128 lowercase hexadecimal characters. Assertion fields are exactly:

`installation_id`, `binding_id`, `enrollment_id`, `enrollment_role`,
`helper_image_digest`, `engine_image_digest`, `engine_version`, `model_id`,
`load_epoch`, `weight_files`, `weights_manifest_digest`, `host_instance_id`,
`engine_process_instance`, `session_id`, `wire_apis`, `cancellation`, `challenge`,
`issued_at`, `expires_at`.

Prepared assertions add exactly `request_id`, `body_digest`, `operation`. Assertions last at
most 60 seconds, reject expiry without grace, and allow at most one second of
future issuance skew. All digests use `sha256:` followed by 64 lowercase hex digits.

The weight manifest is a name-sorted, unique, nonempty array of at most 256
`{name,size,sha256}` objects. Names are relative normalized POSIX paths without
parent traversal. Sizes are bytes; hashes cover actual file bytes. The aggregate
is SHA-256 of the canonical manifest array. An Ollama model-manifest digest is
never substituted for a weight-file hash. Test-only deterministic vectors are in
[connector-v1.json](../tests/vectors/connector-v1.json), regenerated and verified
by `test_published_vectors_regenerate_and_verify`. The published seed is TEST-ONLY;
production loads a preprovisioned Ed25519 key and has no default key.

## Pilot configuration and limits

Run `sovite helper` or `python -m sovite.connector.helper` with
`SOVITE_HELPER_SETTINGS` pointing to an operator-provisioned JSON file. It contains
these `HelperSettings` fields:

| Field | Meaning |
|---|---|
| gateway_url | Exact wss listener URL |
| ca_file, cert_file, key_file | Gateway trust and per-binding client credentials |
| signing_key_file | Preprovisioned Ed25519 assertion key enrolled for this binding |
| engine_endpoint | Exact origin; literal loopback HTTP or validated HTTPS |
| wire_api | One of `chat`, `responses`, `ollama-chat`; default `chat`; selects the only permitted engine path |
| identity_file | Private supervisor/operator metadata JSON described below |
| weight_paths | Mapping from public manifest names to local actual weight-file paths |
| engine_socket | Optional absolute Unix socket; requires engine_endpoint `http://localhost` |
| engine_ca_file | Required explicit CA file for an HTTPS engine; no system roots |
| engine_token_file | Optional upstream bearer credential, sent only to the fixed engine |
| mode | Must be `development` for this pilot |
| body_limit | Positive bytes, at most 4194304; defaults to that ceiling |

The identity file contains exactly the assertion's static fields, excluding
`weight_files`, `weights_manifest_digest`, `session_id`, `challenge`, `issued_at`,
`expires_at`, `wire_apis` (the helper derives the singleton from settings). It must declare `cancellation: "disconnect-only"`. A supervisor must
atomically update `load_epoch` whenever loaded model/weights change, and
`engine_process_instance` on restart. Helper/engine image digests and version are
operator observations, not inferred from model names. The helper hashes actual
files at startup, retains their observed metadata, and refuses a later file
size/mtime/inode change. This is a development observation, not a hostile-process
or concurrent-write attestation. A generic HTTP engine cannot atomically reserve
its model; this adapter therefore refuses enforced mode even if its pins match.

Only POST to fixed `/v1/chat/completions`, `/v1/responses`, `/api/chat` paths is
possible. Request models must match. Every operation requires an explicit boolean `stream`;
Responses also requires explicit `store: false`. Redirects, proxy discovery, user-selected
routing/headers, remote media/file fetching, provider hosted tools, stored
responses, background jobs and unqualified Responses continuation are rejected.
Message and content-part objects use per-operation key allowlists: chat allows
`role, content, name, tool_calls, tool_call_id`; Ollama allows
`role, content, thinking, tool_calls, tool_name`; Responses messages allow
`type, id, role, content, status`. Parts contain only `type, text` with text types
(`text` for chat/Ollama, `input_text`/`output_text` for Responses). Unknown keys,
media parts, images, sources, documents and attachments fail closed. Responses
function-call/output items and function-only tools have explicit structural
allowlists; their arguments, outputs and JSON schemas remain plain data, including
URL-shaped text or property names. Function tool declarations and model-generated
tool-call data are inert; the
helper executes neither. Unsupported request fields fail closed. HTTPS uses
hostname and CA verification, TLS 1.3 minimum, no compression or key logging.
Both TLS clients construct `SSLContext(PROTOCOL_TLS_CLIENT)` directly and load
only explicitly configured CA files; `SSLKEYLOGFILE`, `SSL_CERT_FILE`, and
`SSL_CERT_DIR` do not affect them. Empty gateway CA configuration, or empty CA
configuration for an HTTPS engine, is refused. Deployment egress restrictions remain necessary.

An independent local watchdog expires permits after at most three seconds and
stops a generation at its deadline, on cancel or on disconnect. Invoke and permit
frames carry `permit_ttl_ms`, an integer in `1..3000`. On receipt the helper replaces
the permit deadline with `now_monotonic + permit_ttl_ms / 1000`; equal or smaller
TTL values are valid and each renewal replaces the prior deadline. Wall-clock
expiry and strictly increasing timestamp checks do not apply to permits. A renewal
cannot resurrect expired authority. Identity expiry and the overall request
`deadline` still use whole Unix seconds. After stopping, watchdog and expired
renewal paths recheck request identity and invoked/streaming phase before sending
an error, so a concurrent cancellation/completion cannot receive a stale error. The helper sends heartbeats every two seconds
and stops if gateway messages are absent for six seconds. Output has a one-second
send timeout and bounded buffers; stream cancellation is independent of output
backpressure. A `cancelled` response follows termination of the local HTTP
operation. The cooperative fake engine tests observe actual peer EOF. This does
**not** establish a real provider's generation stop or production six-second
cancellation bound. A provider may keep generating after disconnect; qualification
must measure this, or a cooperating/supervised adapter must replace this pilot.

Tests exercise local TLS, mTLS, messages, identity and a cooperative fake engine.
Real host images, loaded weight reservation, packet/CNI restrictions, real engine
stop bounds, private artifact persistence, authority checks, certificate issuance,
rotation and revocation are later-PR/deployment evidence and have not been run.
