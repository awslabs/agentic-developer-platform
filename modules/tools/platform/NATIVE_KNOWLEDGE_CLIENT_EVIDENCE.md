# Original native Knowledge client transport and operation evidence

This source slice depends on draft PR #8193 and its draft #8166 runtime
dependency. It provides installed client transport and a read-only operation
observer. It does not enable the live Knowledge evaluator in
`tests/e2e/tools/runner.py`, change AC04's 22 cells, or supply live fixtures.

## Existing session to installed client

`NativeSession.export_client_transport` writes a new private file for the
already exchanged original session. The export checks the original finite
status allowance, retained funding, run identity and five-minute dispatch
horizon. It does not issue an approval, exchange another session or refresh
credentials. The file contains secrets and belongs outside the disposable
client HOME in an owned private directory; never publish it.

The private file binds the original reference, access token, capability and
authoritative run start to an explicit finite plan. Each Knowledge read pins
the tool name and canonical argument digest. Each model entry pins its actual
upstream model ID, API route, request count, request bytes, output tokens and
response bytes. Discovery has a separate finite allowance. Only the five
stateless Knowledge read tools in the admission dependency are supported.

The installed `adp_native_test.py` helper is opt-in through
`ADP_NATIVE_TEST_TRANSPORT_FILE`. Both the installed MCP bridge and model proxy
use it. The original native capability and operation ID travel in headers;
they do not enter Knowledge arguments. Each attempted send is recorded under
the original run in `attempts.json`, locked across processes and fsynced before
the transport returns send credentials. The journal binds the complete private
configuration and stores digests and owned receipt references, not payloads
or credentials.

Missing replies, malformed completions, changed bindings, concurrent attempts
and exhausted limits refuse subsequent work. Model completion requires the
terminal API event and integer usage within the requested output limit. The
native model proxy buffers at most the configured response limit and records
completion before delivering any bytes, so an immediate MCP call cannot race
the model's journal update. Ordinary proxy responses continue streaming.
The Responses stream uses the documented
[`response.completed` event](https://developers.openai.com/api/docs/guides/streaming-responses).
An unknown send retains its original operation ID; changing request bytes or
restarting the client cannot clear it. Cleanup has a separate ten-minute
horizon and at most two attempts for a confirmed original owned execution.
Cleanup confirmation does not establish billing settlement.

`native_client_transport` verifies installed helper bytes against the original
approved source revision and checks the isolated installation's gateway. It
starts one local proxy, validates its published process and capability, removes
ambient model/cloud credentials from the child environment, and launches the
native Claude Code or Codex binary through that proxy. `client_matrix.native_call`
accepts that exact launch descriptor and retains its existing transcript and
usage checks. Proxy teardown signals only the process it created, using a
pidfd. The original private transport and journal remain available for review.

These are client request guards. They are not a server aggregate reservation,
a complete provider price contract, proof of provider cancellation, or a bound
on downstream Door work after a client stops reading. Ordinary CLI transport
continues through its existing path when the opt-in variable is absent.

## Independent original-operation records

`NativeKnowledgeOperationObserver` performs six consistent, single-attempt
reads from the real authority and facade execution tables: the original
approval, operation, authority, execution index, facade operation and facade
execution. It requires agreement on original owner, tenant, job, source,
request, quote, target binding, reservation and execution. It compares the
mediator's Door response/RPC digests with the facade result after removing only
the interceptor's receipt metadata. Closed executions also require tracked,
resolved operations and cleanup timestamps within the original deadline.

The returned evidence contains references and digests only. It explicitly
reports `backend_invocation_count: null`, `index_verified: false`,
`billing_complete: false` and `governance_complete: false`. Missing or corrupt
records are unavailable evidence. A missing record cannot establish zero
backend calls. This observer is not AC04's complete backend, index or billing
oracle and is not wired into the live evaluator as one.

## Release prerequisites

- Independently review and adopt draft #8166, #8193 and this stacked source.
  Their integration branches are not deployment authorization.
- Supply genuine owner-approved sessions and protected Knowledge registration,
  source, price and enforced-quantity inputs. The Search registrar cannot
  compile those Knowledge inputs.
- Establish enforceable downstream Door work and occupancy bounds, including
  unknown responses and cancellation. Client byte limits and elapsed time
  alone cannot establish the complete cost bound.
- Establish the aggregate budget contract across AC04's two authentic
  owner/tenant controls. Per-cell estimates and separate local transport plans
  are not a shared reservation.
- Preserve the original worker ACL authority. A designated human's native
  capability does not authorize the worker control.
- Supply independent backend invocation, index, authorization and complete
  billing evidence before wiring the full live evaluator or claiming AC04.

Validation uses offline protocol fixtures, store records and actual local
proxy processes. It invokes no native model, live Knowledge backend or cloud
mutation and establishes no live acceptance result.
