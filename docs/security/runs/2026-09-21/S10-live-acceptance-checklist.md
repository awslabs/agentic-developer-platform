# S10 — live acceptance checklist

Parent #5609; source continuation #5972; revalidation PR #5890.
All live items remain **pending** until receipts are recorded. Unit tests,
controller settings, and old success logs do not substitute for these probes.
Record UTC time, account/region, image digest, source commit, source workload,
target host/path/method, request ID, response status/body digest and matching
gateway audit reason. Store credentials and sensitive response bodies privately.

## 1. Deployed source and target

Confirm account 000000000101/us-east-1 for dev and record the actual gateway
Deployment, namespace, API Gateway stage URL and ClusterIP service. Inspect the
running image/source for provenance enforcement, `hmac.compare_digest` in
`src.internal.auth_deps._verify_internal_key`, and absence of the unused
`src.internal.routes._verify_internal_key`. Record immutable image digests.
Source inspection establishes deployed code, not timing indistinguishability.

Status: [ ] Pending

## 2. Paired identity probes through the same mounted route

Use a disposable, pre-provisioned provider identity and tenant with a known
resolution response. Request bodies must be valid; GET, 404, 405, 422 and unrelated
403 responses are not authentication evidence. For example, POST the following
body to the confirmed `/internal/v1/resolve-user` route:

```json
{"provider":"slack","provider_user_id":"S10_FIXTURE_WORKSPACE:S10_FIXTURE_USER","org_id":"S10_FIXTURE_TENANT"}
```

Run paired requests with the same body/target and record each result:

| Caller | Required result |
|---|---|
| Anonymous | Authentication rejection; no resolution or mutation |
| Human JWT that succeeds on its normal human endpoint | Internal-plane rejection; valid JWT is not machine authority |
| Spoofed privileged `X-Caller-Identity`, absent/wrong edge provenance | Rejected with matching auth reason; no registry authority or shared-key fallback |
| Registered tenant-scoped `shared`/`personal` caller, valid SigV4 | Internal-plane scope rejection even with forged org/scope headers |
| Approved internal/platform caller, valid SigV4 through AWS_IAM route | Exact 200 fixture identity; authenticated principal matches server registry |

Use the deployment's existing SigV4 request helper with the approved caller's
credentials. Do not copy the provenance secret into an external client: API
Gateway supplies/replaces that header. Capture whether edge IAM rejected the
request before the application or the application rejected provenance; these
are distinct evidence. Check the corresponding anonymous/public route mapping
strips caller assertions and cannot manufacture an authenticated context.

Status: [ ] Pending

## 3. Paired ClusterIP shared-secret callbacks

From an approved callback workload, actually POST the same fixture body to the
ClusterIP route, with no identity assertion. Read the key via its existing secret
reference without printing it or placing it in a shell command line.

Require exact 200 fixture resolution for the configured key; repeat with an
invalid key and require the canonical 403. Repeat with a forged identity header
and the valid shared key and require provenance rejection. Correlate request IDs
with gateway logs. Existing logs alone do not establish current behavior.

Status: [ ] Pending

## 4. Network enforcement, with reachable controls

Record deployed CNI enforcement mode and NetworkPolicy selectors. Then perform
actual bounded connection/HTTP probes from the namespaces and service accounts
selected by the policy: one denied agent workload and one approved callback.
Probe the gateway ClusterIP and direct pod/ALB paths covered by policy, and the
approved API Gateway path. Require denied paths to fail while a paired permitted
control reaches the same destination and valid application route. A timeout
without a reachable control may mean an outage, not isolation. Capture source pod
UID/IP, resolved destination, port, duration and result. Remove disposable probes.
Controller presence and policy YAML alone are insufficient acceptance evidence.

Status: [ ] Pending

## 5. Edge policy and coordination

Read the actual REST API resource policy and stage deployment. Record its absence
explicitly rather than treating `None` as acceptance. Associate policy restrictions
with A03 #5655; prefix rate limiting with A14 #5670; execution containment with
S15 #5614 and tenant resolution with S11 #5610. Record whether any residual is
covered by an independently verified control or remains open.

Status: [ ] Pending

## Completion

Close #5609 only after every applicable dev acceptance item has receipts and
residual ownership is explicit. Source continuation #5972 may be source-ready
while live acceptance remains pending. Production is a separate target requiring
its own account, deployed-source and request/network evidence.
