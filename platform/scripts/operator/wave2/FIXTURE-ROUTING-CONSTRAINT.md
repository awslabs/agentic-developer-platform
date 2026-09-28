# Fixture edge routing: what is reachable, and what is not

Issue #3968, Wave 2. Written for root's review before any live execution.

Root's blocker 7 asked for "actual human-session/trusted-edge fixture routing and
an executable orchestration path … verify auth-NONE human bearer versus AWS_IAM
internal paths correctly; do not fake caller headers."

**Status: both planes are now fixture-scopable.** The human bearer path always
was. The AWS_IAM internal path was not, and this document previously recorded it
as "not achievable inside this task's scope" — correctly, when written. That
verdict is now obsolete: **#5836 built the owning change this document named**, so
the constraint below describes the ORDINARY edge, and is no longer a limit on the
evaluation.

The ordinary-edge analysis is kept rather than deleted, because it is the reason
the fixture edge has to exist at all, and because it is still exactly what
happens to a worker pointed at the ordinary endpoint by mistake — which is why
the launcher checks for that specifically.

## The constraint (on the ORDINARY edge — still true)

The internal agent routes are gated on an edge-injected assertion that a fixture
pod cannot receive from the ordinary edge.

1. `modules/gateway/src/agentauth/routes.py:50-57` — every `/internal/v1/agent`
   route carries `Depends(require_agent_transport)`, which 403s on a missing
   `X-Caller-Identity` and otherwise defers to `verify_internal_or_irsa`.
2. `modules/gateway/src/auth/caller_provenance.py:89-101` — the assertion is
   believed only when `settings.trust_apigw_headers` is true **and**
   `X-Adp-Edge-Provenance` `compare_digest`-matches `settings.apigw_provenance_secret`.
   Presence of `X-Caller-Identity` is terminal (#3985): a failed provenance check
   is a 403, never a fallback to the shared secret.
3. Only API Gateway injects that pair, on the three `AWS_IAM` routes
   (`modules/gateway/infra/modules/api-gateway/main.tf:299-300, 318-319, 351-352`
   with the shared `local.verified_caller_identity` at `:200-204`).
4. Those routes are `http_proxy` + `VPC_LINK` to **one** ALB ARN supplied as a
   Terraform variable (`main.tf:372-378`, `variables.tf:89-99`). The worker's
   `ADP_AGENT_CONTROL_ENDPOINT` resolves to `/internal/{proxy+}`, which reaches
   `local.internal_plane_alb_arn` → Ingress `bedrockgateway-internal` → Service
   `bedrockgateway:80` → **pods labelled `app: bedrockgateway` only**
   (`modules/gateway/k8s/service.yaml:11-12`).

So genuine-provenance traffic **on the ordinary edge** terminates at the ordinary
Deployment's pods, by label. None of the ways of bending that edge toward a
fixture is available:

| Candidate | Status | Evidence |
|---|---|---|
| Host-header routing | does not exist — both Ingresses are host-less | `k8s/ingress.yaml:106-115`, `ingress-internal.yaml:96-108` |
| API GW stage variables | none declared | no `variables` block on `aws_api_gateway_stage.main`, `main.tf:637-668` |
| Canary / weighted stage | none | no `canary_settings` anywhere in `modules/gateway/infra/` |
| A second route to another backend | needs a Terraform body change → new deployment | paths are a literal at `main.tf:236-421`; `sha1(body)` redeploy trigger at `:622-625` |
| Second Ingress joining the same ALB | **confirmed impossible on this cluster** | `group.name` is a documented no-op under EKS Auto Mode, `k8s/ingress.yaml:60-73`; grouping lives in `IngressClassParams` and changing it *replaces* the ALB |
| Repoint the Service selector at the fixture | would hijack ALL ordinary traffic | forbidden: mutates an ordinary object |

That table is why the answer was never "modify the ordinary edge". It was "build
a separate one" — which is what happened, in its own issue with its own review,
rather than smuggled in under an evaluation ticket.

## What changed: #5836's fixture edge

This document's own closing section ("The exact owning change, if fixture-scoped
internal verification is wanted") named three things. `agent/issue-5836` delivers
all three, in `modules/gateway/infra/fixture-edge/`:

1. **A separate REST API**, not a route added to the ordinary one — so the
   ordinary API's body, its `sha1(body)` redeploy trigger and its
   `lifecycle.postcondition` are untouched. It carries both planes:
   * an `AWS_IAM` internal plane injecting `X-Caller-Identity` from
     `context.identity.userArn` plus a provenance header, and
   * an auth-`NONE` human plane injecting a **blank** caller identity, so a
     client on that plane cannot acquire provenance by using it.
   Its `lifecycle.postcondition` requires every route to map both headers, so a
   half-wired plane cannot be added to it later either.
2. **A fixture-owned internal ALB** — a dedicated Ingress
   (`fixture-alb.yaml.tmpl`) whose single `/internal` path backs the **fixture's**
   ClusterIP Service, the one `render_fixture.py` already emits. That is the whole
   difference: on this edge, genuine-provenance traffic terminates at fixture
   pods. It is a dedicated ALB because, per the table above, Auto Mode cannot
   share one.
3. **The endpoint to set**, as the `worker_control_endpoint` output:
   `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/internal/v1/agent`
   — already the https bare-host shape `run_identity.py:657` demands, already
   carrying the `/internal/v1/agent` prefix the pod registers its routes under,
   and `run_identity.py:665` appends `/bootstrap` to it.

Two properties of that component matter to this evaluation specifically:

* **The provenance secret is fresh per run**, held only in a per-run SecureString
  SSM parameter and deliberately omitted from Terraform outputs (an output marked
  `sensitive` is still plain text in state, so omission was required, not
  marking). The fixture therefore does not share production's secret, so the two
  edges are isolated *from each other*: a header valid at one is not valid at the
  other.
* **The ALB is bound to the run by tag.** `main.tf`'s `run_binding_gate` refuses
  to plan unless the discovered ALB carries `AdpFixtureRun = <run_nonce>`, which
  is what stops the edge from being attached to ordinary infrastructure or to
  another run's ALB.

### Coordination with #5836 — which side owns what

| Thing | Owner | Note |
|---|---|---|
| Fixture REST API, stage, resource policy, provenance SSM parameter | #5836 | destroyed by `fixture-lifecycle.sh destroy` against an isolated per-run state key |
| Fixture internal ALB (its Ingress) | #5836 composes it; **#3968's ledger records it** | through the existing `record-k8s --kind Ingress` interface — #5836 modified no file in this directory |
| Per-run provenance Secret on the fixture pod | #5836 `handoff` creates and attaches it; **#3968's ledger records it** | `record-k8s --kind Secret` |
| Fixture Deployment, Service, NetworkPolicies, worker Job | #3968 (`lib/render_fixture.py`, `10-create-fixture.sh`) | |
| Teardown of every Kubernetes object above | #3968 `90-cleanup-ledger.sh`, uid-gated | |

**Ordering is load-bearing in both directions.** The edge must be destroyed
BEFORE the fixture Ingress: `main.tf` reads the fixture ALB as a data source and
Terraform re-reads data sources during destroy, so deleting the ALB first makes
the destroy unplannable. `90-cleanup-ledger.sh` is what deletes that Ingress, so
it must not run before `fixture-lifecycle.sh destroy`.

### The create ordering, and why the fixture is built in two stages

The destroy ordering above has a mirror image on the way up, and it is the reason
`10-create-fixture.sh` and `run-all.sh` take `--stage`.

The fixture edge is built **in front of** the fixture's ClusterIP Service: its
Ingress backs that Service, and `main.tf` reads the resulting ALB as a data
source. So the Service (and the Deployment behind it) must already exist before
#5836 can plan. But the worker Job cannot be created until the edge exists,
because `ADP_AGENT_CONTROL_ENDPOINT` is required with no default and a worker
started without it raises "endpoint is not configured" before any bootstrap
attempt. The two requirements point in opposite directions:

```
fixture Deployment + Service  ──needed by──>  #5836's edge
        #5836's edge          ──needed by──>  worker Job
```

There is therefore no single invocation that can create the whole fixture. The
sequence is `--stage gateway` → #5836 `fixture-lifecycle.sh apply` / `handoff` →
`--stage worker`, with **one shared ledger** across all three, so that teardown
still sees every object exactly once and still deletes by uid.

Two properties of that split matter here:

* **The endpoint is consumed as a run-bound receipt, not as a URL.** The worker
  stage takes `--edge-receipt`: #5836's **whole** `terraform output -json`
  document, because the bindings live in its `ownership` output while the
  endpoint is a separate top-level one — `terraform output -json ownership`
  cannot supply an endpoint at all, and neither can the `ownership.json` their
  `apply` writes. `lib/edge_receipt.py` refuses the document unless its
  `run_nonce`, `account_id`, `region` and `environment` match this run's ledger,
  and then **parses** the endpoint: the host must be exactly
  `<rest_api_id>.execute-api.<region>.amazonaws.com` and the path exactly
  `/<environment>/internal/v1/agent`, with no userinfo, port, query or fragment.
  Checking that the API id merely *appears* in the URL is not enough — it also
  admits a non-AWS host wearing the id as a label, another region's execute-api
  host, and the id sitting in the path of an unrelated host. The production
  bootstrap client does not require an AWS host, so this is the only place that
  gap is closed. The ordinary-API check described below is kept on top of all of
  it, not replaced by it.
* **Between the stages the fixture is running.** The gateway stage forces
  `--keep-fixture` and exits non-zero, because leaving a control-flag-ON fixture
  gateway and a live fixture queue up is the cost of the split and must not read
  as a completed run. `lib/stage_gate.py` decides admission for the second
  invocation by comparing observed uids against the ones this run recorded, so a
  prerequisite that was adopted, replaced, or deleted between stages is refused
  with its own reason rather than silently reused.

## What this means for the evaluation

**Achievable — worker→gateway bootstrap against the fixture.** With the #5836
endpoint the protected bootstrap roundtrip is exercisable against an isolated
fixture, and `10-create-fixture.sh --stage worker --worker-job` now performs it
instead of refusing. The safety property the old refusal provided is kept by a check rather
than by absence: `--worker-control-endpoint` is **required** with no default, and
the value supplied is compared against the ORDINARY gateway's API id read from
SSM (`/adp/<env>/gateway/api-gateway-id`). A value on the ordinary API is
refused, and an *unreadable* parameter is also refused — an unreadable check is
not a passed check.

**Achievable — the auth-NONE human bearer path.** Unchanged, and it never needed
the edge. It does not go through `require_agent_transport` at all; it resolves
through `src/auth/dependencies.py`, where a request that asserts *no*
`X-Caller-Identity` skips the provenance branch entirely (`dependencies.py:207`)
and authenticates on its JWT. `auth_deps.py:170-172` records the same property
for the shared-secret callers that reach the pod by ClusterIP without transiting
API Gateway. So a real human session is directly testable against the fixture
over its ClusterIP, with no fabricated headers — and the gateway→worker control
dial on 8770 is gateway-initiated and needs no edge assertion at all.

Note the asymmetry that makes this safe to assert: a *forged* assertion is not
merely useless against the fixture, it is actively rejected. On the internal
routes a failed provenance check is a 403 (`auth_deps.py:181-194`), and on the
human routes it falls through to the JWT branch (`dependencies.py:203-206`)
rather than granting anything. Neither path can be talked into accepting a header
the edge did not write — and that stays true of the fixture edge, whose secret is
a different value from production's.

What remains **not** fixture-scoped is the AWS_IAM internal path measured against
the **ordinary** edge: that is an observation of a deployed configuration, and
`40-verify-edge-sessions.sh` marks it `fixture_scoped: false`. That flag is a
statement about which edge a given measurement used, not a claim that the path
is unmeasurable — the same logical path IS fixture-scoped when driven through
#5836's API.

Anything stronger on the ordinary edge would require fabricating
`X-Caller-Identity`, which root excluded and which the code would reject anyway.

## The worker Job: the conditions it must satisfy

The gateway's verifier enumerates them
(`modules/gateway/src/agentauth/workload.py:139-155`). A fixture worker pod is
refused unless all of the following hold:

* namespace `adp-agents` and SA `agent-authority-worker-sa` — and note these come
  from the ORDINARY gateway's env (`workload.py:85-92`,
  `AGENT_WORKER_NAMESPACE` / `AGENT_WORKER_SERVICE_ACCOUNT`), so a fixture cannot
  relocate them without also overriding the verifying gateway's own config;
* exactly one container, named `agent-worker`, in both spec and status;
* **no `command` and no `args`** — a debug shell entrypoint is refused, so the
  Job must run the real entrypoint;
* container env contains exactly `[{ADP_AGENT_AUTHORITY_ENABLED: "true"}]` — a
  **list equality** check, so a duplicate entry, a `valueFrom`, or `"True"` all
  refuse, while Kubernetes itself accepts all three;
* `imageID` digest ∈ `AGENT_WORKER_IMAGE_DIGESTS`, so the image must be
  digest-pinned to an already-approved digest — this fixture cannot introduce a
  new one (that is a Terraform-gated allowlist,
  `webhook-ingress/infra/variables.tf:520-528`);
* pod phase `Running`, non-empty `status.podIP`, no `deletionTimestamp`;
* and `ADP_AGENT_CONTROL_ENDPOINT` must be set at all — without it
  `run_identity` / `task_gateway_client` / `status_gateway_client` all raise
  "endpoint is not configured" before any bootstrap attempt. An earlier
  revision's published fixture Job omitted it.

These conditions split in two, and the tooling splits the same way rather than
pretending a template can promise everything:

* **Spec-decidable** — namespace, SA, container count and name, absence of
  `command`/`args`, the exact authority env list, the projected token audience,
  read-only credential mounts, a digest-pinned image on the approved list.
  `lib/render_fixture.py::assert_verifier_admissible` refuses these BEFORE
  producing an object, naming the condition, because each one otherwise surfaces
  as an opaque "workload refused" only after the operator has paid for the setup.
* **Runtime-observable** — `phase == Running`, the RESOLVED `imageID`, a
  non-empty `podIP`, exactly one `containerStatus`. `lib/worker_observation.py`
  reads these from the API server after creation. Asserting them from a template
  would be a guess dressed as a check.

### Who reads the pod, and why it is not the worker

The **operator**. Root verified the protected service account can neither `get`
nor `list` pods, and that boundary is preserved rather than widened to make a
test helper work: there is no in-worker `kubectl` here and no RBAC grant to make
one work. The split is

* the **container** receives its own `metadata.uid` through a downwardAPI
  volume — one of the five fieldRefs such a volume supports (`annotations`,
  `labels`, `name`, `namespace`, `uid`). It does **not** support
  `spec.serviceAccountName`, which is why an earlier revision's dependence on a
  `service-account.name` file could never have worked, and why `metadata.uid` is
  the viable in-container proof;
* the **operator** resolves what that uid's pod really is — service account,
  container, resolved image digest, address, and the scoped role ARN read from the
  service account's own `eks.amazonaws.com/role-arn` annotation rather than named
  by hand — and writes it into the expected-identity document.

The experiment therefore compares a value it cannot forge against a reference it
did not author. Neither half alone is an identity: a subject that writes its own
reference has asserted nothing, and a reference with nothing to compare against
admits everything.

The pod is bound to the Job by `ownerReferences` (kind `Job`, matching
server-assigned uid, `controller: true`), never by label match: a label is
wearable by anything, and a same-named Job from an earlier run has a different
uid. This is also why the Job is rendered with `backoffLimit: 0` — a replacement
pod is a different uid, so a silent retry would leave the recorded identity
describing a pod that no longer exists while another ran unobserved.

One further composition constraint, from the ordinary side: the fixture worker
must **not** wear `app.kubernetes.io/name: agent-scaledjob`. Both ordinary worker
NetworkPolicies select exactly that label (`scaledjob-netpol.tf`:
`agent-scaledjob-egress`, `agent-control-listener-ingress`), so wearing it would
place a control-enabled fixture pod inside a production allowlist — widening an
ordinary boundary to make a fixture convenient. The fixture gets its own
run-scoped policies instead.

## Why this is written down instead of worked around

The three workarounds that would have produced a green result are all
dishonest: fabricate the two headers; repoint the ordinary Service at the
fixture; or run the "fixture" as the ordinary Deployment with the control flag
on. The first is explicitly forbidden and the code rejects it, the second is an
outage, and the third destroys the isolation the fixture exists to provide and
would breach DP-INV-1.

That reasoning is also why the remaining gap was written down as a named,
reviewable Terraform change with a real cost rather than worked around — and so
it could then be built, reviewed and paid for in #5836, instead of appearing
inside an evaluation as a convenience.

The evaluation still reports only what was actually verified. Whatever is not
exercised is `not_run`, which is nonzero and never a pass.
