# Gateway Responses streaming

`POST /openai/v1/responses` sends keep-alives to clients while waiting for model
output. The upstream HTTP read timeout is independent of those keep-alives.

## Configuration

`BG_MANTLE_STREAM_READ_TIMEOUT_SECONDS` sets the maximum upstream quiet period.
The default is **600 seconds**, and the accepted range is greater than zero and
at most 3600 seconds. Set it through the deployment's gateway environment
configuration when an override is needed. All environments use the same default
from `Settings`; there is no account-specific patch or Terraform resource change.

Streaming connect and connection-pool waits are bounded at ten seconds. The
existing non-streaming request and streaming write timeout remains 120 seconds.
The read timeout is an idle limit, not a total generation limit; receiving model
bytes resets it. Downstream SSE keep-alives are sent every 15 seconds at record
boundaries. Binary Bedrock streams retain whole-frame ping keep-alives.

## Failure signals

After HTTP headers have been sent, a failure cannot change the client's HTTP
status. At a complete SSE record boundary the gateway sends an `error` event:

- `upstream_stream_timeout`: the model stopped sending bytes past the read limit.
- `upstream_stream_error`: the upstream connection failed.
- `upstream_stream_incomplete`: upstream EOF arrived without a terminal event.

The error includes a gateway request ID, without exposing provider exception
details. The gateway does not invent `response.completed` or replay a partially
delivered request. If bytes from an incomplete event have already reached the
client, the connection closes rather than inserting an error into that JSON.

The structured `stream_outcome` log field distinguishes `completed`,
`incomplete`, `failed`, `error`, `read_timeout`, `transport_error`,
`premature_eof`, `client_cancelled`, and unexpected `interrupted` exits.
`upstream_status` records the original HTTP response. `status_code` records the
outcome used by usage logging (504 for read timeout, 502 for upstream failure or
missing terminal event, 499 for client cancellation). Provider usage already
received remains available to the existing settlement path.

Correlate by `request_id`, not an HTTP 200 access-log entry. A successful terminal
`response.completed` event is required for a `completed` stream outcome.

## Validation and rollout

Run the gateway proxy suite, including `test_mantle_stream_lifecycle.py`. It uses
a local HTTP server to exercise real HTTPX read deadlines, along with terminal
event, cancellation, framing, and byte-fidelity regressions. Keep-alive tests cover
LF/CRLF/CR delimiters, split delimiters, upstream `TimeoutError`, and binary frames.

Promote the gateway application image through the normal release workflow.
Verify a short Responses request and a generation with a quiet period exceeding
120 seconds, and check that a deliberately interrupted local test emits an
explicit error without a success log. Increasing the limit cannot repair a
provider that remains stalled; the configured upper bound still applies.

The 17 September incident had 31 upstream `ReadTimeout` failures in a six-hour
window. A confirmed Astra request (`490a1fcb-e7cc-4f30-87ea-fc81027f2170`) failed at
19:18:01 UTC in the upstream body iterator with the previous 120-second read
limit. The old cleanup path nevertheless logged `mantle stream completed: HTTP
200`. This change also incorporates the keep-alive corrections identified in
#4897, including tracking delimiters that span HTTP chunks.
