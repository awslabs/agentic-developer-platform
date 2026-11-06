# Door requests to the private Zoekt runtime

Door search, source-file listing, and whole-file reads use one authenticated
transport. Requests carry `q` and `opts`; the private auth proxy validates and
normalizes those fields before forwarding them to Zoekt. Missing or placeholder
backend keys fail before the Door sends a request. The proxy removes the bearer
key before forwarding and continues to expose only search and source-free health.

The transport and proxy enforce these per-request settings:

| Setting | Ceiling and behavior |
| --- | --- |
| `Opts.MaxWallTime` | 10 seconds, encoded as integer nanoseconds; smaller positive values are retained |
| `Opts.MaxDocDisplayCount` | 500 files, or the smaller requested limit |
| `Opts.ShardMaxMatchCount` | 100,000 matches; the provider's existing default made explicit |
| `Opts.TotalMaxMatchCount` | 1,000,000 matches; the provider's existing default made explicit |
| `Opts.Whole` | Preserved for whole-file reads |
| Request JSON | At most 65,536 bytes at both Door and proxy |
| Response JSON | At most 4 MiB at Door; unexpected compression is refused |

The Door has a total asynchronous request deadline as well as HTTP I/O timeouts.
The proxy binds its original upstream request context to the selected deadline.
It replaces absent, zero, or excessive option values with its finite ceilings;
negative values, unsupported fields, query-string options, oversized bodies, and
trailing JSON are rejected before contacting the provider. No automatic retry or
redirect is enabled. Search failures still raise `ZoektUnavailable`; browse
retains its existing empty-result failure handling.

## Pinned provider proof

The [image Dockerfile](images/zoekt/Dockerfile) pins revision
`153817f643cde8b229ee388c1dddbcf07f4798af` and archive SHA-256
`94d67e89e7407a5a2afd8481245ab0b252a43867eff4d818552ef8acd9f0d03d`.
These observations come from that exact source:

- [`web/server.go`](https://github.com/sourcegraph/zoekt/blob/153817f643cde8b229ee388c1dddbcf07f4798af/web/server.go#L243)
  mounts the JSON server under `/api/`.
- [`internal/json/json.go`](https://github.com/sourcegraph/zoekt/blob/153817f643cde8b229ee388c1dddbcf07f4798af/internal/json/json.go#L30)
  decodes `Q`, `RepoIDs`, and `Opts`. The previous top-level `num` was ignored.
  This API uses a 20-second context default when `MaxWallTime` is zero; the HTML
  handler's separate default does not describe Door's API.
- [`CalculateDefaultSearchLimits`](https://github.com/sourcegraph/zoekt/blob/153817f643cde8b229ee388c1dddbcf07f4798af/internal/json/json.go#L124)
  can issue an additional estimator search when a document display limit is set
  without a shard match limit. Supplying both prevents this hidden extra search.
- [`SearchOptions`](https://github.com/sourcegraph/zoekt/blob/153817f643cde8b229ee388c1dddbcf07f4798af/api.go#L971)
  defines the duration, display limits, and match limits. Display limits truncate
  collated results; they do not limit documents examined.
- [`search/shards.go`](https://github.com/sourcegraph/zoekt/blob/153817f643cde8b229ee388c1dddbcf07f4798af/search/shards.go#L1019)
  applies `MaxWallTime` to engine work. The scheduler is acquired earlier; the
  proxy deadline also cancels the original HTTP request while queued. Match
  stopping occurs after shard results arrive and can overshoot with concurrent
  work already in flight.

[Transport tests](tests/unit/test_zoekt_transport.py) inspect all three Door HTTP
paths for consumed options, authentication, byte limits, total deadline, and
absence of retries or redirects. [Proxy tests](images/zoekt/auth-proxy/main_test.go)
import the actual pinned JSON server. A recording Searcher proves what its
decoder passes to `Search`, including exactly one search when options are bounded
and the ignored legacy `num` behavior. Local HTTP servers separately prove that
client cancellation and the proxy deadline cancel the original upstream request.
These tests do not claim measured resource usage by a populated index.

The image build runs these tests inside the pinned provider source with the
repository's `go.mod` and `go.sum`. The same arrangement can be checked outside
Docker using `go test -mod=readonly -race -count=1 -timeout=60s ./adp-auth-proxy`.

## Remaining work before aggregate guarantees

Public read eligibility, tenant filtering, catalogue collision checks, and result
provenance remain at the existing Door ACL boundary. This change does not enable
write or memory operations.

These are per-request limits and cancellation signals, not a complete operation
budget or proof of settled occupancy. A single Door operation can still fan out
through Zoekt, SQL, S3, and graph queries. Catalogue and object reads, graph
fallbacks, SQL result counts, backend retries, and queue occupancy need separate
limits. Timing out an HTTP client or executor future does not prove all downstream
work has finished. Whole-file results may exceed the transport byte cap and then
fail without being presented as complete content.

Aggregate guarantees require an original-operation-bound durable claim before
each backend attempt, bounded fan-out and queues, verified backend completion or
cancellation before releasing occupancy, and trusted usage settlement. No prices,
funding reservation, genuine-session evidence, or cross-owner budget contract are
established by this source change. Live acceptance remains gated on those inputs.
