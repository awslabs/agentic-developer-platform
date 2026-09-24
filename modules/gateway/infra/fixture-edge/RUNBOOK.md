# Fixture trusted edge — operator runbook (Issue #5836)

For **root**, to execute under the existing account authorization after PR review.
Target: **879318057152 / dev / us-east-1 / embark1**.

Everything below is the manual equivalent of what a push-triggered workflow would
do. Nothing in this repo runs it automatically, and the component is **default-off**
(`fixture_edge_enabled = false`), so reviewing and merging the PR creates nothing.

> **What this delivers, and what it does not.** This edge is the missing transport
> that lets a protected worker complete a **genuine** bootstrap against an isolated
> fixture gateway — the change #3968's `FIXTURE-ROUTING-CONSTRAINT.md` identified and
> deliberately left unbuilt. The repository tests are mocked (`terraform test`,
> plan-only); they pin configuration properties and **do not** establish live
> acceptance. Steps 5–7 below are what produce live evidence.

---

## 0. What exists, and the one thing this adds

The ordinary edge injects the two trusted headers only on its `AWS_IAM` routes, and
all of them forward to a single ALB that terminates at pods labelled
`app: bedrockgateway`. That is why an isolated fixture is unreachable today, and why
no amount of configuration on the ordinary API fixes it.

This component adds a **second, disposable REST API** whose signed route forwards to
the *fixture's* ALB and injects a *fixture-specific* proof. Consequences worth
knowing before you start:

| | Ordinary edge | Fixture edge (this) |
|---|---|---|
| REST API | `bedrockgw-dev-api` (`59o2rakc50`) | new, per-run, name carries the run nonce |
| Terraform state | ordinary gateway state | **isolated backend key** (step 2) |
| Provenance secret | `/adp/dev/gateway/apigw-provenance-secret` | `/adp/dev/gateway/fixture/<nonce>/apigw-provenance-secret` |
| Lifetime | permanent | deleted with the run |

The two secrets are different values, so a header minted by one edge is **rejected**
by the other gateway. Isolation runs in both directions.

**Prerequisites, and who owns each.** An earlier revision of this runbook said
#3968 creates the fixture's internal ALB. That was **wrong** — its renderer emits a
Deployment, a ClusterIP Service and NetworkPolicies and **no Ingress** — which left a
circular prerequisite where neither side created the ALB this edge forwards to.

| Thing | Owner | How |
|---|---|---|
| fixture Deployment + Service | #3968 | `10-create-fixture.sh` |
| fixture **internal ALB** | **this component** | `scripts/create-fixture-alb.sh` (step 1b) |
| the trusted edge | this component | `scripts/fixture-lifecycle.sh` |
| fixture **NetworkPolicy** | #3968 renders it | this component publishes the one fact it cannot know — the ALB's traffic source (step 4.5) |
| ledger + teardown of k8s objects | #3968 | `ownership.py` / `90-cleanup-ledger.sh` |
| `wave2_preflight` / `teardown_verification` **artifacts** | #3968 assembles them | this component emits the two entry fragments for its own resources (step 8.5) |

On this EKS Auto Mode cluster one Ingress is one ALB and changing IngressGroup
identity *replaces* the ALB, so a dedicated fixture Ingress is the only way to give
the fixture its own balancer without disturbing the live one. That is a real per-run
cost, and it is why the ALB is created by an explicit step rather than conjured.

---

## 1. Read-only preflight

Nothing here mutates. Confirm the target and collect the four discovered inputs.
Run these with the `adp-embark1` credential.

```bash
# 1a. Confirm the account. Everything keys off this.
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
# EXPECT Account = 879318057152. If it is anything else, STOP.

# 1b. The reused VPC link, and the security group the fixture ALB must reuse.
#     The link's SG does not have open egress: in dev sg-013f2ce2bcaf1642c permits
#     tcp/80 to three specific groups only. A fixture ALB outside that set would
#     pass every other check and then time out on every request.
aws apigatewayv2 get-vpc-links --region us-east-1 \
  --query 'Items[].{Id:VpcLinkId,Name:Name,Status:VpcLinkStatus,SGs:SecurityGroupIds}' --output table
# EXPECT bedrockgw-dev-vpc-link-v2 AVAILABLE -> vpc_link_id  (dev: qmovr6)

# You need ONE of these for --alb-security-groups in step 1.5. It is NOT a
# Terraform input: read the note below before copying the ids anywhere.
aws ec2 describe-security-group-rules --region us-east-1 \
  --filters Name=group-id,Values=<link-sg-id> \
  --query 'SecurityGroupRules[?IsEgress==`true`].{To:ReferencedGroupInfo.GroupId,Proto:IpProtocol,From:FromPort,Until:ToPort}' \
  --output table
# dev: tcp 80-80 -> sg-0623ec399f4a20b87, sg-0d76484377ffc964d, sg-0b0f5533ab8440db8

# 1c. The ORDINARY internal-plane ALB, recorded so the config can refuse to target it.
#     BOTH values are required — an absent one is not a passed check.
aws ssm get-parameter --name /adp/dev/gateway/internal-plane-alb-arn \
  --query Parameter.Value --output text   # -> ordinary_internal_plane_alb_arn
aws ssm get-parameter --name /adp/dev/gateway/internal-plane-alb-dns \
  --query Parameter.Value --output text   # -> ordinary_internal_plane_alb_dns

# 1e. The protected worker role that will call the fixture edge.
aws iam get-role --role-name adp-dev-agent-authority-worker-role \
  --query 'Role.Arn' --output text
# -> allowed_caller_role_arns  (dev: arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker-role)
```

**Why 1c matters.** A "fixture" edge pointed at the ordinary ALB would inject genuine
trusted headers and forward them to the **live** gateway pods, producing a green
result while actually exercising production traffic. The configuration refuses that
combination at plan time, but only if you supply this value.

**Why the security groups from 1b are NOT a Terraform input.** An earlier revision
took them as `vpc_link_egress_target_security_group_ids` and proved reachability by
intersecting that list with the ALB's discovered groups. Root's review rejected
that, and rightly: the list records what you saw *at the moment you ran the command*
and nothing keeps it true. It also only ever described the **egress** half — the
inbound rule lives on the ALB's group and can be missing or scoped to another port,
which is a state a correctly-built fixture reaches and which that check could not
see. So `main.tf` reads the rules itself (`data.aws_vpc_security_group_rule`, every
group attached to either side) and refuses unless it finds a live rule for **both**
directions on the fixture port. You still need one id from 1b, for step 1.5's
`--alb-security-groups`: that is the group the ALB is *told to reuse*, which is a
different thing from evidence that the reuse works.

If the gate refuses for reachability, **do not add a rule.** Both groups on the path
are shared — `sg-0623ec399f4a20b87` is carried by both ordinary gateway ALBs — and
#5836 forbids ordinary-infrastructure changes. Point the fixture ALB at a group that
already works, or stop and escalate. This component declares no security-group
resource, so it cannot make that change even if asked to.

---

## 1.5 Create the fixture's own internal ALB

**This mutates the cluster.** Run it after #3968's `10-create-fixture.sh` has created
the fixture Deployment and Service, and before the Terraform steps — the edge reads
this ALB, so it must exist first.

```bash
./scripts/create-fixture-alb.sh \
  --run-id "<#3968 run id>" --run-nonce "$FIXTURE_NONCE" \
  --account-id 879318057152 --profile adp-embark1 \
  --ledger "<#3968 ledger.json>" \
  --namespace adp-gateway --service "<fixture Service name>" \
  --alb-security-groups "<from 1b>"
```

`--check-only` renders and server-side dry-runs it, creating nothing.

It uses `kubectl create` (never `apply`, which would **adopt** a same-named object
and let the ledger authorise deleting something this run did not create), records the
server-assigned uid via #3968's `ownership.py record-k8s`, waits for the ALB, then
verifies on the **live** resource that the scheme really is `internal` and the
`AdpFixtureRun` tag really is this nonce — the tag the Terraform gate refuses to plan
without. It prints `fixture_alb_arn` and `expected_vpc_id` for step 3.

### Everything that can refuse, refuses before the create

`kubectl create` is the one call in this script that cannot be undone, so every
check that could stop the run happens above it. Four things were previously checked
too late, or on evidence too weak to carry the conclusion.

**`--account-id` is now required, and verified first.** The previous revision read
`sts get-caller-identity` purely to fill in the ledger row's `--account-id`, four
steps *past* the create. So the account the run was acting on was unknown at the
moment it mutated, and a credential pointing elsewhere produced a real Ingress — and
therefore a real ALB — before failing. Naming the expected account is what turns that
read into a refusal: an ambient value resolves to itself and cannot disagree with
itself.

**The cluster is identified the same way `fixture-lifecycle.sh` does it.** `--profile`
binds the AWS CLI and says nothing about which cluster `kubectl` will mutate, and a
context *name* is a local alias that can read
`arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks` while pointing at any server.
What is compared is the kubeconfig API server **endpoint** against the endpoint AWS
reports for the named cluster, read through this run's own profile. Pass
`--expect-cluster <name>` when your context is an alias — it is a name to look up,
not a string to echo back.

**The fixture Service must be *this run's*, not merely a name that exists.** The old
check was `kubectl get service <name>`, which proves the name is taken. An ordinary
Service, or another run's fixture, satisfied it — and the Ingress would then have
routed the trusted edge's header-injecting traffic into whatever that was. The script
now reads the Service as a document and requires **both** of #3968's renderer labels
(`adp.io/w2-fixture` = this run id, `adp.io/w2-nonce` = this nonce), `type: ClusterIP`,
and that the port the rendered backend references is actually exposed. Both labels,
because an earlier attempt under the same run id but a different nonce is still a
different run, and the nonce is what every other gate in this component binds to.

### A partial creation is recoverable by uid, and only by uid

A create can succeed server-side and still report a failure — dropped connection,
killed process, a ledger write that fails afterwards. The previous revision left
nothing on disk in that case, so an Ingress (and its ALB) could exist with no trace
and no ledger row.

Two records now bracket the create, both in the run's private `700` artifact
directory (`.fixture-run-<nonce>/`, shared with `fixture-lifecycle.sh`):

| File | Written | What it proves |
|------|---------|----------------|
| `alb-intent.json` | **before** the create | an attempt was made, by *this* run/account/name |
| `alb-receipt.json` | immediately after, before the ledger call | the **server-assigned uid** of the object created |

```bash
# Finish an interrupted run without deleting or re-creating anything.
./scripts/create-fixture-alb.sh --recover \
  --run-id "<#3968 run id>" --run-nonce "$FIXTURE_NONCE" \
  --account-id 879318057152 --profile adp-embark1 \
  --ledger "<#3968 ledger.json>" \
  --namespace adp-gateway --service "<fixture Service name>"
```

`--recover` re-reads the live object and records it **by its actual uid**, refusing
unless that uid still equals the one captured at creation. A uid changes only on
delete-and-recreate, so a differing one means the object under this name belongs to
something else. It also **requires the uid receipt**: the intent proves an attempt was
made, it cannot prove which object now holds the name, and recording is what
authorises deletion. With no receipt the only safe outcome is escalation, so recovery
refuses rather than adopting whatever is live.

**No failure path tells you to delete the Ingress by name.** The old messages did
(`kubectl delete ingress <name> -n <ns>`), and that is the single most dangerous
instruction here: a same-named Ingress may be another run's, and deleting an Ingress
deletes its load balancer. Every failure now points at `--recover` or at reading the
uid first. This is asserted over every reachable failure scenario in
`tests/test_fixture_alb_composition.py`, because what matters is what the *operator*
sees.

The Ingress publishes **both** planes the edge routes — `/internal` for the trusted
plane plus `/me`, `/auth` and liveness for human sessions. An earlier revision served
`/internal` alone, which left the edge's auth-`NONE` route forwarding to a listener
default action: the human-plane positive control in step 6 could not pass, because the
backend published nothing for it to reach. See
[the human probe path](#the-human-probe-path-must-be-one-the-fixture-alb-publishes).

No file under `platform/scripts/operator/wave2/` is modified. An Ingress is an
ordinary uid-bearing Kubernetes object, so #3968's existing `k8s` ledger bucket and
uid-gated delete path already cover its teardown; no new ledger type was needed.

> **Teardown order, since this is where the ALB is created:** destroy the **edge
> first**, then delete this Ingress. An earlier revision of the script's closing note
> said the opposite. It is not a documentation nit — `main.tf` reads this ALB
> (`data.aws_lb.fixture`) and Terraform re-reads data sources during `destroy`, so
> deleting the Ingress first makes the edge's destroy *unplannable*, and the only
> apparent way forward is deleting cloud resources by hand — the one thing the
> ownership gates exist to prevent. Full procedure in
> [§8 Teardown](#8-teardown-in-dependency-order-with-proof).

---

## 2. Isolated state, bound to an explicit account and run

Every step below is run through `scripts/fixture-lifecycle.sh`, which refuses to
guess any of the bindings. The prose version of this procedure was not executable —
that is what this script fixes.

```bash
cd modules/gateway/infra/fixture-edge

# The SAME nonce the #3968 ownership ledger generated for this run.
export FIXTURE_NONCE="<nonce from platform/scripts/operator/wave2 lib/ownership.py>"

./scripts/fixture-lifecycle.sh init \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 \
  --region us-east-1 --environment dev \
  --profile adp-embark1 --state-bucket <terraform state bucket>
```

It asserts the live credential really resolves to `879318057152` before doing
anything, binds every AWS call to `--profile`, and uses a state key that carries
**both** the account and the nonce:

```
fixture-edge/dev/879318057152/<nonce>/terraform.tfstate
```

Two concurrent runs cannot corrupt each other, and a mistyped bucket belonging to
another account cannot collide with that account's fixture state.

### What every later command re-checks about that binding

`init` records the resolved backend in `$TF_DATA_DIR/terraform.tfstate` (or
`.terraform/`). Every subcommand that touches state — `plan`, `apply`, `handoff`,
`verify`, `destroy` — re-reads that record and **refuses** unless four things match
its own command line. Checking only the state *key* was not enough:

| Checked | Why the key alone missed it |
|---------|------------------------------|
| `key` | the original check: nonce + account, so `init --nonce A; plan --nonce B` is refused |
| `bucket` | the same key in **another bucket** is a different state file entirely — a typo, or another account's state bucket this credential can reach, passed while reading foreign state |
| `type` | an `s3` record replaced by a `local` one still carries a matching key, and local state has none of the per-run isolation teardown's ownership story rests on |
| `profile` | the backend's profile is the identity that **reads and writes** the state; if it differs from `--profile`, the plan is built from state that the run's account/cluster checks were never made against |

#### The expectation comes from `init`, not from your command line

Each of those comparisons needs something to compare *against*, and taking it from the
current command line does not work: **a flag you omit cannot disagree with anything.**
`init` pointed at someone else's state bucket followed by a `plan` with no
`--state-bucket` exited **0** and planned against that foreign state, because with no
expected bucket supplied there was no comparison left to fail. The same hole existed for
`--profile`. A check that is waived by leaving an argument out is not a check.

So `init` writes `backend.init.receipt.json` into the artifact directory, recording the
bucket, key, type and profile it actually resolved — and every later command takes its
expectation from **that**, unconditionally. Flags you *do* pass become a cross-check: a
`--state-bucket` that contradicts the receipt is refused by name rather than silently
preferred. The receipt's own run binding (nonce, account, region, environment) is
validated before anything is read out of it, so a receipt from another run cannot supply
the expectation used to admit that run's state.

Practical consequence: run `init` for each run, and keep its artifact directory. If the
receipt is missing you will be told to re-run `init` — that is deliberate, because the
alternative is inferring the expectation from the very thing being checked.

### `--profile` binds the AWS CLI; the provider is bound separately

`terraform` has **no `--profile` flag** — its AWS provider resolves credentials from
the process environment. So a run whose `aws` calls are all correctly bound can still
have `plan`/`apply`/`destroy` act on a *different* account, which is the exact split
the account guards exist to prevent (guards run through the CLI, mutations through
the provider).

Every terraform invocation therefore goes through a wrapper that exports
`AWS_PROFILE=<--profile>` **and clears the higher-precedence variables**
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
`AWS_DEFAULT_PROFILE`, `AWS_CREDENTIAL_PROFILES_FILE`). Setting `AWS_PROFILE` alone
is not sufficient: static keys outrank it in the SDK chain, so an operator or CI job
with another account's keys already exported would have terraform use **those** while
`aws --profile` kept reporting the correct account and every guard passed.

### The cluster is pinned by its API endpoint, not its context name

`handoff` and `recover-secret` mutate Kubernetes, and `--profile` does not bind
`kubectl` at all. A kubectl **context name is a local label** chosen by whoever wrote
the kubeconfig: it can read `arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks`
while pointing at anyone's API server, so parsing it proves nothing.

The script instead reads the resolved endpoint
(`kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'`) and
compares its host against `aws eks describe-cluster --query cluster.endpoint` for the
named cluster, **through this run's bound profile and region**. That both confirms the
cluster exists in the authorised account and pins the connection kubectl will really
make.

`--expect-cluster` is the **EKS cluster NAME** to verify against AWS — not a string
compared to the context name. (In the previous revision it was the latter, which made
it a free-form bypass: echoing back `kubectl config current-context` always satisfied
it.) Pass it whenever the context is an alias:

```bash
./scripts/fixture-lifecycle.sh handoff ... --expect-cluster adp-dev-eks
```

With an EKS-ARN-shaped context the name is taken from the ARN as a convenience — the
verification against AWS still runs, because the ARN is also just a local string.

> **There is no "just keep state locally" alternative.** An earlier revision of this
> step said you could omit the `-backend-config` flags and keep state locally. That
> is false, and was reproduced as false on Terraform 1.15.3: with a `backend "s3"`
> block declared, omitting them **fails** init —
> `Error: Missing Required Value — The attribute "key" is required by the backend.`
>
> The only two modes are the real run above, and **checks only**:
> `terraform init -backend=false` (supports `fmt`/`validate`/`test`, **not**
> `plan`/`apply`). If truly local state were ever wanted the backend block would have
> to be deleted, not under-configured — and that local file would hold the provenance
> secret in plain text, which is a further reason the secret is not a Terraform output.

---

## 3. Retained inputs, in a private directory

The script keeps run artifacts in `.fixture-run-<nonce>/`, created **700** and
verified to be 700 (a pre-existing loose directory is normalised, not accepted).
Write the reviewed inputs there, so what was applied is auditable:

```bash
ART=".fixture-run-${FIXTURE_NONCE}"
mkdir -p "$ART" && chmod 700 "$ART"

cat > "$ART/fixture.tfvars" <<EOF
fixture_edge_enabled = true

run_nonce           = "${FIXTURE_NONCE}"
expected_account_id = "879318057152"
aws_region          = "us-east-1"
environment         = "dev"

# From step 1b — the script prints these two for you.
fixture_alb_arn = "<create-fixture-alb.sh output>"
expected_vpc_id = "<create-fixture-alb.sh output>"

# From step 1c. Both REQUIRED: a skipped isolation check is not a passed one.
ordinary_internal_plane_alb_arn = "<from 1c>"
ordinary_internal_plane_alb_dns = "<from 1c>"

vpc_link_id = "qmovr6"

# NOTE there is no security-group input. Reachability is read from the live rules
# (both directions, on the listener port) — see "Why the security groups from 1b
# are NOT a Terraform input" in step 1.

allowed_caller_role_arns = ["arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker-role"]
EOF
```

Note there is **no `fixture_alb_dns` input**: the DNS name is read from the load
balancer named by `fixture_alb_arn`, so the two cannot disagree. This file contains
no secrets — the provenance proof is generated by Terraform and never appears in
inputs, outputs or logs.

---

## 4. Plan, review, then apply the reviewed plan

```bash
./scripts/fixture-lifecycle.sh plan \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1
```

The plan is machine-reviewed before you see it: any change to a resource type
outside this component's expected set **stops the run**. That should be impossible
(separate state, separate API), so it means the wrong backend or directory.

**Expect exactly these**, all name-bound to the nonce:

| Resource | Purpose |
|---|---|
| `terraform_data.run_binding_gate[0]` | blocking isolation preconditions |
| `aws_api_gateway_rest_api.fixture[0]` | the fixture edge |
| `aws_api_gateway_rest_api_policy.fixture[0]` | wrong-role refusal on `/internal` |
| `aws_api_gateway_deployment.fixture[0]` | |
| `aws_api_gateway_stage.fixture[0]` | stage `dev` |
| `aws_cloudwatch_log_group.fixture[0]` | 7-day access logs |
| `aws_ssm_parameter.fixture_provenance_secret[0]` | SecureString handoff |
| `random_password.fixture_edge_provenance[0]` | (no cloud resource) |

A plan-time **refusal** here is the gate working. `terraform_data.run_binding_gate`
fails the plan — exit code 1, not a warning — if the caller's real account or region
disagrees, or if the fixture ALB is public, in another VPC, not tagged for this run,
or is the ordinary internal-plane ALB.

Two of its preconditions are about **reachability**, and they are stated separately
because the fix differs:

| Refusal | What is missing | What to do |
|---|---|---|
| "no live security group rule lets the VPC Link egress to the fixture ALB" | no rule on any of the link's groups permits tcp/`<port>` to any group the ALB carries | point the ALB at a group the link already egresses to (step 1.5 `--alb-security-groups`) |
| "the fixture ALB's security groups do not admit the VPC Link" | egress exists; no **inbound** rule on the ALB's groups admits the link's group on that port | same fix — a group that admits the link. This half is invisible until traffic flows, so a refusal here has saved you a timeout |

Both messages print the group ids on each side and the `describe-security-group-rules`
command that reads what the gate read. Neither is fixable by editing a variable: there
is no variable. **Do not add a rule to either group** — see step 1.

```bash
./scripts/fixture-lifecycle.sh apply \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1 \
  --plan-file ".fixture-run-${FIXTURE_NONCE}/fixture.plan"
```

`apply` takes a **reviewed plan file only**; it will not generate a fresh plan. It
writes an ownership receipt to the artifact directory afterwards, and from that
receipt a **creation-ledger fragment** for #5825's W2-10 — see step 8.5. Without it
this edge's resources sit outside the check that proves the evaluation left nothing
behind, and their later absence observations are refused as naming resources the
fixture never created.

### "A plan file" is not "the plan you reviewed"

`plan` writes `fixture.plan.receipt.json` next to the plan, and `apply` recomputes it
and refuses any difference, naming the field. This is not belt-and-braces: a saved
plan file records no run nonce, no account, no backend and no trace of the variables
it was built from, so `--plan-file <path>` alone could not distinguish

| What you might hand it | Caught by |
|---|---|
| another run's `fixture.plan` (two runs sharing one `--artifact-dir`) | `run_nonce` |
| a plan built while pointed at another account/region/environment | `account_id`, `region`, `environment` |
| a plan built against another state bucket, key or credential | `state_bucket`, `state_key`, `state_profile` |
| `fixture.tfvars` edited after review | `tfvars_sha256` |
| the plan file itself modified after review | `plan_sha256` |

Both halves are load-bearing: the digest alone proves only that *a* file is
unmodified, and the binding alone is satisfied by any plan for the same run.
Terraform will happily apply a saved plan whose var-file has since changed — the plan
carries its own values — so `tfvars_sha256` is what makes "reviewed" mean the inputs
too. `--dry-run` runs the same check, so a dry run cannot report "would apply" for a
plan the real apply would refuse.

If you genuinely need to change something, re-run `plan` and review what it writes.
Do not edit the receipt; it is the record of what was reviewed, not a config file.

---

## 4.5 Hand #3968's fixture NetworkPolicy the ALB's traffic source

**Do this before step 6, not after a failed step 6.** Skipping it produces a fixture
that plans cleanly, reports its ALB targets healthy, and then refuses every request
at the pod — which reads as "the protected worker failed its bootstrap", the exact
conclusion Wave 2 exists to reach on its own merits.

**Why it is needed.** #3968's `render_fixture.render_policies` gives the fixture
gateway one ingress rule, and it admits sources **by namespace**:

```yaml
from:
  - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: adp-gateway}}
  - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: adp-agents}}
ports: [{protocol: TCP, port: 8080}]
```

A `namespaceSelector` matches **pod** sources. With target-type `ip` the ALB connects
from its **own** elastic network interfaces, which belong to the load balancer and to
no pod and no namespace. So that rule admits exactly the sources a pure in-cluster
harness uses, and denies the edge this component builds. Both sides are individually
correct; the gap is in the composition, which is why it is a step here.

**Read the source after apply:**

```bash
terraform output -json fixture_alb_network_policy_source
```

It publishes `source_cidrs` (one `/32` per interface), `container_port` (**8080**),
`alb_listener_port` (80 — a *different* number), the `run_nonce`, the `alb_arn`, and a
`verify` command that re-derives the same addresses with the AWS CLI alone:

```bash
aws ec2 describe-network-interfaces \
  --filters Name=description,Values="ELB <fixture alb arn_suffix>" \
  --query 'NetworkInterfaces[].PrivateIpAddress'
```

That description is how the ELB service labels an ALB's own interfaces, and it is what
`main.tf` filters on — so the output and this command are reading the same fact, not
two guesses. The apply **refuses** if the list comes back empty (an `ipBlock` matching
nothing denies the edge while the policy reads as configured) or if any matched
interface is outside `expected_vpc_id`.

**Add ONE ingress rule — to the FIXTURE policy only:**

```yaml
- from:
    - ipBlock: {cidr: <source_cidrs[0]>}
    - ipBlock: {cidr: <source_cidrs[1]>}   # one entry per address
  ports:
    - {protocol: TCP, port: 8080}          # container_port, NOT alb_listener_port
```

### The four ways to get this wrong

| Tempting fix | Why not |
|---|---|
| relax the **ordinary** gateway's policy | forbidden by #5836 (ordinary flags, routes and selectors are preserved) and it widens production's blast radius for a fixture |
| `ipBlock: 0.0.0.0/0` on the fixture policy | admits the whole VPC and every other ALB in it — a wider allowance than the ordinary plane itself has |
| the **subnet** CIDR | both ordinary gateway ALBs share `subnet-03ae2ea2ebdf611bb` with this one, so a subnet rule admits *production's* edge to the fixture |
| another `namespaceSelector` | cannot work at any width: the source is not a pod |

`port: 80` is the fifth: the ALB **listens** on 80 and **connects to the pod** on
8080. A rule naming 80 blocks precisely the flow under test. The output carries both
numbers under distinct names for that reason, and a test asserts they differ.

### Ownership, and what this component does not do

This component **observes**; #3968 **renders**. Nothing here creates, edits or reads a
NetworkPolicy, and no file under `platform/scripts/operator/wave2` is touched — the
two changes compose through that one output value. Neither side can widen the other:
a rule built from this value admits exactly these addresses, on exactly the fixture
gateway's pods, on exactly the container port.

**Re-read it if the ALB is recreated.** The addresses are the ALB's *current*
interfaces; one gained (a subnet or AZ added) makes the list stale. Staleness is safe
in the direction that matters — a missing address is a denial, never an admission —
but it is a denial that looks like a broken handshake, so re-run the output rather
than trusting a value pasted from an earlier run. The `run_nonce` in the output is
there to make a pasted stale value visibly not this run's.

---

## 5. Hand the fixture gateway its secret — and actually attach it

```bash
./scripts/fixture-lifecycle.sh handoff \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1 \
  --ledger <#3968 ledger.json> \
  --namespace adp-gateway \
  --fixture-deployment w2-fixture-gateway-<run-id>
```

**Why `--fixture-deployment` is mandatory.** An earlier revision created the Secret
and stopped. That changed nothing: the fixture pod kept reading the **ordinary**
gateway's `bedrockgateway-secrets/apigw-provenance-secret`, so it validated against
production's value. The Secret alone is inert. This step:

1. reads the per-run SSM parameter and **refuses** any path that is not
   `.../fixture/<nonce>/...`, so the two edges cannot share one trust root;
2. pipes the value SSM → `kubectl` stdin — never a file, variable, log or argv — and
   checks the **SSM exit status separately** from `kubectl`'s, because a Secret built
   from a failed read would hold an error string and surface much later as an
   unexplained 403;
3. refuses to adopt an existing Secret, and records the server-assigned uid in the
   #3968 ledger, failing loudly (with the manual `kubectl delete` to run) if that
   record cannot be written;
4. **attaches** it with a strategic-merge patch that repoints
   `BG_APIGW_PROVENANCE_SECRET` and sets `BG_TRUST_APIGW_HEADERS=true` on the fixture
   Deployment, and then **verifies** all nine secret-backed env refs survived.
   `container.env` merges by name, so the eight refs #3968's renderer deliberately
   carried over are preserved; a json-merge patch would replace the whole list and
   silently drop them.

`BG_TRUST_APIGW_HEADERS=true` is defensible on the fixture **only because this
component blanks both trusted headers on its auth-NONE route**. Never set it on the
ordinary gateway.

The pod must roll for any of this to take effect:

```bash
kubectl rollout status deployment/w2-fixture-gateway-<run-id> -n adp-gateway
```

---

## 6. Verify the endpoint's identity — before pointing a worker at it

This is the step that proves isolation rather than assuming it.

```bash
ENDPOINT="$(terraform output -raw worker_control_endpoint)"
API_ID="$(terraform output -raw rest_api_id)"
echo "$ENDPOINT"
# EXPECT https://<API_ID>.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent
```

**6a. It must NOT be the ordinary API.**

```bash
test "$API_ID" != "59o2rakc50" && echo "OK: distinct from the ordinary edge" || echo "STOP"
```

**6b. It must resolve to the fixture pods, not the ordinary ones.** Confirm the
fixture ALB's target group holds the fixture pod IPs:

```bash
aws elbv2 describe-target-health \
  --target-group-arn "$(aws elbv2 describe-target-groups \
      --load-balancer-arn '<fixture_alb_arn>' \
      --query 'TargetGroups[0].TargetGroupArn' --output text)" \
  --query 'TargetHealthDescriptions[].{Target:Target.Id,State:TargetHealth.State}' --output table
# Cross-check against: kubectl get pods -n adp-gateway -l <fixture selector> -o wide
```

**6c. The refusals — and a positive control.** Each must be observed, not assumed,
and each must be attributed to **the layer that actually refused it**. A 403 proves
nothing if you cannot say which component produced it; three different mechanisms are
involved here and only one of them is this component's resource policy.

| # | Request | Expected | **Which layer refuses, and why** |
|---|---|---|---|
| 1 | unsigned, on `/internal` | 403 | **API Gateway `AWS_IAM`** — no valid SigV4, rejected before any policy or pod is consulted |
| 2 | unsigned + forged trusted headers | 403 | **Same as #1.** The forged headers are irrelevant: the request never reaches the pod, and the edge would overwrite both anyway |
| 3 | correctly signed, role NOT allow-listed | 403 | **This component's resource policy** — the explicit `Deny` on `/internal`, matched by `aws:PrincipalArn` |
| 4 | forged provenance header that *did* reach the pod | 403 | **The gateway pod** — `src/auth/caller_provenance.py` constant-time compare against the per-run secret |
| 5 | **human bearer session on a non-`/internal` route** | **NOT 403** | **Positive control.** Must succeed; see below |

```bash
# 1. UNSIGNED -> refused by API Gateway's AWS_IAM, before the policy or the pod.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ENDPOINT/bootstrap"

# 2. SPOOFED headers, unsigned -> same layer as #1; the edge also OVERWRITES both.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ENDPOINT/bootstrap" \
  -H 'X-Caller-Identity: arn:aws:iam::879318057152:role/anything' \
  -H 'X-Adp-Edge-Provenance: forged'

# 3. WRONG ROLE, correctly signed -> refused by THIS component's resource policy.
#    Use a signed identity NOT in allowed_caller_role_arns. Do NOT use the worker role.
awscurl --service execute-api --region us-east-1 -X POST "$ENDPOINT/bootstrap" -d '{}'
```

Confirm #3 was the *policy* and not something else, by reading the access log rather
than inferring it from the status code:

```bash
aws logs tail "/aws/api-gateway/bedrockgw-dev-w2fx-${FIXTURE_NONCE}" --since 5m
```

**5 — the positive human control, and why it must be run.** The previous revision's
resource policy denied everything under `execution_arn/*`, including the auth-`NONE`
human routes. An unsigned human request carries no `aws:PrincipalArn`, so it matched
the `Deny` and was refused **at the edge, before the gateway could authenticate the
JWT at all**. The policy is now scoped to `/internal` only, and this control is what
proves it: a human bearer-token request on a non-`/internal` route must be handled by
the gateway (200/401/403 *from the application*), **not** refused by the edge.

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://${API_ID}.execute-api.us-east-1.amazonaws.com/dev/health"
# EXPECT a normal application response, NOT an edge refusal. If this is refused by
# API Gateway, the Deny has regressed to API-wide and human sign-in is broken.
```

### The human probe path must be one the fixture ALB publishes

`/dev/health`, **not** `/dev/api/health`. Two independent reasons, and an earlier
revision of this runbook (and of the test suite) got both wrong, so the positive
control it documented would have returned 404 against a real edge:

* **No `/api` prefix.** That prefix belongs to the ordinary front door, where
  CloudFront strips it before the ALB. There is no CloudFront in front of a fixture
  edge, so `/api/...` reaches the ALB literally and matches no rule.
* **The stage root is already accounted for.** `/dev` here is the API Gateway stage;
  everything after it is forwarded to the ALB unchanged. `verify` takes the path
  *after* the stage, so `--human-probe-path` is `/health`, never `/dev/health`.

The paths the fixture ALB serves are enumerated in `fixture-alb.yaml.tmpl`, and each
is traced to the router that actually mounts it:

| Path | Match | Serves |
|------|-------|--------|
| `/internal` | Prefix | the trusted plane — reachable **only** through the `AWS_IAM` route |
| `/me` | Prefix | `/me/budget`, #3968's session probe endpoint. Mounted on a **prefix-less** `APIRouter` in `src/budget/me_routes.py`, so `/me` is the prefix to publish |
| `/auth` | Prefix | `src/auth/routes.py` (`APIRouter(prefix="/auth")`), including `/auth/me` |
| `/activity/invocations` | Prefix | the agent-control endpoints `#5825`'s merged evaluator calls: `.../{id}/agent/{ping,state,<verb>}` (`src/activity/routes.py:817,837,857` on a **prefix-less** router) |
| `/orchestration/runs` | Prefix | the **second** control adapter the same evaluator calls: `.../{id}/{ping,state,<verb>}` (`src/orchestration/controls.py`, `prefix="/orchestration"`) |
| `/admin/agent-run-stats` | Exact | the endpoint `#3968`'s `31-seed-and-count.py:166` reads its seeded counts back through |
| `/health`, `/ready` | Exact | liveness, registered at the app root in `src/app.py` |

The last three were **missing**, and their absence did not degrade the evidence — it
made it unobtainable. Each request reached the listener's default action and returned
404, which those collectors record as a failed control-plane probe. Two details of
how they are now published are deliberate:

* **Both control adapters, not just `/activity`.** `agent-control-eval.py`'s
  `ADAPTERS` declares two HTTP edges onto the one control service and six of its
  checks iterate both. That pairing exists precisely to show the two edges have not
  drifted, so a 404 on one side does not half-pass the check — it voids it.
* **Scoped to the route space, not to its first segment.** A bare `/admin` Prefix
  would publish every admin router the pod mounts (identity recovery, persona-model
  defaults and posture, bedrock routing, access-request approve/deny, member
  budgets); a bare `/orchestration` Prefix would publish approval gates and node
  resume/recovery. No acceptance step calls any of them, and a fixture exposing more
  surface than its evidence needs is a wider blast radius for nothing.

Publishing them does **not** widen the signed plane. The edge's `AWS_IAM` integration
forwards to `/internal/{proxy}` — it *prepends* the prefix — so a signed caller cannot
address these paths through it at all. (Note this differs from the **ordinary** edge,
whose `/agent/{proxy+}` route *strips* its prefix; if this component's integration ever
changed to match, the published set would need re-reviewing. There is a test asserting
it has not.) All seven human-plane routes authenticate on the pod's JWT — verified
against `get_current_user` on each, with the four `POST` verbs additionally refusing a
non-human principal in `authorize_human_session`.

`verify` refuses an unpublished `--human-probe-path` **before** sending anything,
reading the permitted set out of the template rather than a second hardcoded list
that could drift from it. That refusal is not pedantry: an unpublished path answers
from the listener's default action, and the resulting 404 is indistinguishable from
the edge misrouting human traffic — one is an operator typo, the other is the defect
this control exists to detect.

There is deliberately **no `/` rule**. A catch-all would forward every path the pod
serves — `/internal` included — to a target group the auth-`NONE` route also reaches,
i.e. an unsigned path to the internal API, destroying the isolation argument.

Expected: **403 for 1–4, and a normal application response for 5.** Capture each
status code plus its attributed layer as evidence. A `200` on 1–4, or an edge refusal
on 5, means STOP. A `5xx` on 1 or 2 also means STOP: it can indicate the request
reached a backend, which those two must never do.

---

## 7. Point the protected worker at it

Set on the **fixture worker Job only**:

```
ADP_AGENT_CONTROL_ENDPOINT = <worker_control_endpoint, from step 4's outputs>
```

The worker appends `/bootstrap` itself and rejects any endpoint that is not HTTPS
with a bare host, which is why the published value is an HTTPS execute-api URL
already ending at `/internal/v1/agent`.

The fixture worker pod must still satisfy the gateway's workload verifier, and those
constraints are **read from the verifying gateway's own environment** — so the
fixture gateway's `AGENT_WORKER_NAMESPACE` / `AGENT_WORKER_SERVICE_ACCOUNT` must
match where the Job actually runs, and the image must be digest-pinned to an
already-approved digest. The Job must run its real entrypoint (no `command`/`args`)
and carry exactly `ADP_AGENT_AUTHORITY_ENABLED=true`. Those requirements are owned by
the #3968 fixture tooling; this component only provides the transport.

A successful bootstrap here is the live acceptance evidence for #3968 — **with no
fabricated headers anywhere in the path.**

---

## 8. Teardown, in dependency order, with proof

**Ordering is load-bearing, and it is the opposite of the intuitive one.** `main.tf`
reads the fixture ALB (`data.aws_lb.fixture`) to derive its DNS name, and Terraform
re-reads data sources during `destroy`. So deleting the fixture Ingress first makes
the destroy **unplannable** — reproduced on Terraform 1.15.3:

```
Error: ... data source error — the object cannot be read
```

Therefore: **destroy the edge first** (which stops the listeners and routes), **then**
delete the Ingress and its ALB.

```bash
# Review first — this deletes nothing.
./scripts/fixture-lifecycle.sh destroy \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1 --dry-run

# Then for real.
./scripts/fixture-lifecycle.sh destroy \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1
```

What it does, and what it refuses:

1. **Plans the destroy from exact state** and prints the precise list of objects
   state owns. A plan that would also *create* or *update* anything stops the run.
2. **Checks that plan against the owned set** — see below. A delete-only plan is not
   a plan that deletes only *your* resources.
3. **Refuses to run without the reviewed `fixture.tfvars`.** It will not fall back to
   deleting by name prefix or tag: a name prefix is not ownership, and a same-named
   replacement created by someone else would be destroyed instead. This is why
   replacement-safe teardown needs exact state, not a post-hoc sweep.
4. **Destroys the edge**, then **verifies absence** rather than trusting the summary —
   the REST API, its stage, the per-run secret and the log group must be gone, and the
   **ordinary** provenance parameter must still be present. Every probe addresses the
   id recorded in state rather than a name rebuilt here; see step 8.5 for the
   guessed-prefix defect that made this necessary. Any unverified absence fails the
   command, so cleanup is never reported as complete on an unproven teardown.
5. **Writes the absence observations** as part of tearing down, into
   `teardown-removals-fragment.json` — the other half of the W2-10 evidence, in the
   evaluator's own removal shape. Step 8.5 covers what it contains, why an
   unverifiable resource contributes nothing, and why you must never pre-create or
   hand-edit it.

### A delete-only plan is not a plan that deletes only *your* resources

"The plan contains no creates or updates" is a claim about the **actions**. It says
nothing about the **objects**, and every state file this credential can reach produces
a delete-only plan:

* state that was pulled, copied, or re-initialised under this run's key — it carries
  the *other* run's resource ids;
* state with a resource **imported** into it. One `terraform import` is enough to make
  the ordinary API Gateway, or any log group, a delete-only line in your plan;
* state for the right run in the wrong account.

So the plan is checked against the **owned set**: the `ownership` receipt read back
from state (itself validated against this command's nonce, account, region and
environment *before* any id is taken from it) plus the `AdpFixtureRun` tag the provider
stamped on each object. Two independent facts — the id says state claims the object,
the tag says the object was stamped for this run at creation. An imported resource has
the first and not the second.

#### The match is on `(resource type, id)` pairs, and the ids are the provider's

Two details of that comparison are load-bearing, and both were originally wrong.

**An id alone cannot show every owned resource is included.** `aws_api_gateway_rest_api_policy`'s
id *is* the rest-api id — the policy is an attribute of the API, not a separate object
— so the six owned resources have only **five distinct ids** between them. Compared as
a set of ids, a plan that deletes the API and *not* the policy presents every owned id
and reads as complete coverage. The policy is the wrong-role Deny, so leaving it behind
is not a benign omission. The receipt therefore records each row's Terraform **resource
type** and the comparison is keyed on the pair. A receipt with an untyped row is
**refused** and you are told to re-apply: falling back to an id-only comparison for it
would reinstate exactly that gap.

**The receipt's ids must be the ones a plan actually carries.** The stage's provider id
is `ags-<rest-api-id>-<stage-name>`, *not* the stage name. The receipt previously
recorded `stage_name` (`dev`), an identifier no plan ever contains, so the stage's real
deletion line matched nothing, the guard concluded `NOT OWNED`, and **a legitimate
teardown was blocked** — which is worse than a missed check, because the only apparent
way forward is deleting by hand, the one thing this gate exists to prevent. Every row
now records the resource's own `.id`. If you ever see `NOT OWNED` naming a resource you
recognise as this run's, suspect this class of mismatch before you suspect the state,
and do **not** resolve it by deleting the object directly.

Both directions are refused:

* **`NOT OWNED` / `NOT TAGGED FOR THIS RUN`** — the plan would delete something this
  run does not own. Nothing is deleted. Do not work around it by deleting by name;
  reconcile the state against the receipt.
* **`LEAVES BEHIND`** — the plan omits a resource this run *does* own. That would apply
  cleanly and report a completed teardown while the object keeps running and costing.
  It is caught here, before the state that names the object is emptied; afterwards
  there is no record left to reconcile against. Usually a `state rm` or an earlier
  interrupted destroy.

`random_password` and `terraform_data` are exempt because they have no cloud object at
all. They are enumerated, not inferred, so a newly-added resource type is refused until
someone decides which it is — an unsatisfiable gate is a gate that gets bypassed.

`--dry-run` runs this check too.

If the ALB was already deleted out of order, the destroy plan cannot be read. Use:

```bash
./scripts/fixture-lifecycle.sh destroy ... --recover   # adds -refresh=false
```

That path was verified to complete. It is a recovery, not the normal route.

**Then, and only then, the Kubernetes objects:**

```bash
# #3968's ledger deletes the fixture Ingress (this is what removes the ALB) and the
# fixture Secret, both uid-gated.
platform/scripts/operator/wave2/90-cleanup-ledger.sh --ledger <ledger.json>
```

Finally remove the private artifact directory `.fixture-run-<nonce>/`, which holds the
reviewed inputs and plan files.

**Ownership model, and its honest boundary.** The fixture Ingress and Secret are in
#3968's ledger with their server-assigned uids, so its cleanup deletes them only when
the live object is still the one this run created. The **Terraform-owned** resources
(REST API, policy, deployment, stage, log group, SSM parameter) are owned by the
isolated per-run **state file**, which is a stronger record than either a name prefix
or a ledger row — and they are removed by the reviewed destroy plan above. The
`ownership` output and the receipt in the artifact directory exist so a human reading
the ledger can see these resources and how they are torn down; they are *not* the
delete authority. #3968's `cleanup_ok` stays `None` for a dry run and `False` on any
unverified absence, so do not record success for this component until both the destroy
verification and `90-cleanup-ledger.sh` report verified absence.

## 8.5 The two evidence fragments #5825's W2-10 consumes

**These are not optional paperwork.** The merged #5825 evaluator
(`platform/scripts/agent-control-eval.py`) answers W2-10 — "the evaluation left
nothing behind" — by reconciling `teardown_verification.removals` against
`wave2_preflight.creation_ledger` **in both directions**:

* a ledger entry with **no absence observation** fails as *unaccounted* — "a resource
  nobody looked for is how a control-enabled workload outlives its evaluation";
* a removal naming an identity **the ledger does not contain** also fails, as removing
  something the fixture did not create.

So a resource this component creates and does not contribute is not merely
unrecorded. If you contribute neither half, W2-10 passes with this edge's API, stage,
policy, log group and per-run parameter **entirely outside the accounting**. If you
contribute only the removal, it is *refused* as naming something uncreated.

The lifecycle script therefore writes two files into the artifact directory:

| File | Written by | Belongs in |
|---|---|---|
| `creation-ledger-fragment.json` | `apply`, right after the apply succeeds | `wave2_preflight.creation_ledger` (concatenate `entries`) |
| `teardown-removals-fragment.json` | `destroy`, **as part of** tearing down | `teardown_verification.removals` (concatenate `removals`); also take `captured_at` and this component's `verified_after_teardown` from here |

Each entry is already in the evaluator's own shape —
`(kind, name, identity, created)` and `(identity, absent, observed_by, removed_at)` —
so #3968's preflight/teardown assembler concatenates rather than transforms. Hand both
to that assembler; nothing under `platform/scripts/operator/wave2/` is modified by this
component.

### Why a fragment and not the artifact

`wave2_preflight` is **one** artifact covering the whole run — #3968's fixture
workload, its policies, its queues, and this edge. Writing the whole file here would
mean owning fields that are #3968's to observe (`merged_revisions`, `ci_gates`,
`deployed_components`, `fixture_only_flag_scope`). This component contributes only the
entries for the resources it created.

### `identity` is the provider-assigned id, read from state

Not a name. The evaluator wants an identity precisely because a name cannot
distinguish this object from a same-named replacement. Two consequences you will see
in the files:

* the stage appears as **`ags-<api-id>-dev`**, not `dev` — the provider's own id, and
  the same string the destroy guard matches plan lines on;
* the REST API and its resource **policy share one provider id** (the policy is an
  attribute of the API), so both are qualified as `<type>:<id>`. The evaluator refuses
  a duplicate identity across the whole ledger, and an unqualified collision would be
  reported against #3968's assembler rather than the component that produced it.

### An unverifiable resource contributes **nothing**, on purpose

A probe that could not run — AccessDenied, a throttle, an unreadable id — is a call
that failed, not a resource that answered. It is listed under `unobserved` and
contributes **no** removal entry:

* `absent: true` would be the AccessDenied-reads-as-deleted defect `probe_absent`
  exists to prevent;
* `absent: false` would assert it is **still present**, which was never established.

Leaving it out means the creation ledger's entry goes unaccounted and W2-10 **fails**
it — which is exactly what an unverifiable resource is. Do not synthesise removals for
anything in `unobserved`, and do not report `cleanup_ok` true while that list is
nonempty. `destroy` also exits nonzero in this case, so the two agree.

The fragment is written on the **failing** paths too. Absence evidence is most needed
when teardown did *not* verify: exiting nonzero with no file at all leaves the
assembler unable to tell "not yet run" from "ran and found a survivor". A failing run's
fragment carries `verified_after_teardown: false` and a nonempty `unobserved`, so it
cannot be mistaken for a clean result.

### Freshness is checked by the evaluator, not claimed by the file

The evaluator digests this artifact immediately **before** invoking teardown and
refuses a byte-identical file afterwards, because absence observations written ahead of
the removal describe the fixture while it still existed. It also requires `captured_at`
to be a parseable ISO-8601 instant that does not predate the teardown's start. Hence:

* `destroy` **deletes any previous attempt's fragment before deleting anything**, so a
  failed teardown leaves no fragment rather than a stale one;
* `captured_at` is stamped as the fragment is written, i.e. after the deletion and
  inside the teardown window;
* **never** pre-create or hand-edit either file. A `captured_at` is a string written by
  the same hand as the absence claims; the digest comparison is what makes it evidence.

### Two entries are derived, and say so

`aws_api_gateway_rest_api_policy` and `aws_api_gateway_deployment` have no probe of
their own and must not be given a fabricated one: the policy is an **attribute** of the
REST API and the deployment is its **child**, so once the API is not found there is
neither a policy nor an API through which to query one. Their absence is derived from
the API's own not-found, `observed_by` states the derivation in full, and
`observation` reads `derived-from:rest_api` so a reviewer need not parse prose. Direct
reads carry `observation: "direct"`.

### If you add a resource to `main.tf`, add its probe

An applied resource whose type `destroy` has no probe for is a **refusal**, not a
silent omission — otherwise the fragment quietly accounts for less than the run
created, and the evaluator would blame #3968's assembler for the gap.

Relatedly, and the reason the probe targets are now read from the ownership receipt
rather than recomposed in the script: the log-group probe used to rebuild its own name
as `/aws/apigateway/w2-fixture-edge-<nonce>` while `main.tf` creates
`/aws/api-gateway/<name_prefix>-fixture-edge` — wrong stem *and* a missing hyphen. As
`describe-log-groups` answers an unmatched prefix with an **empty list and exit 0** (the
one probe where a clean exit is read as absence), the real log group was reported gone
on every run and a survivor could not have been detected. Probe targets come from state
now; a fragment entry whose probe read a different object is refused.

## What CI checks before any of this runs

`.github/workflows/fixture-edge-ci.yml` runs on every PR touching this directory:
`terraform fmt -check`, `validate`, the 25 mocked `terraform test` run blocks, the
three pytest suites, and `bash -n` on every script.

It holds **no AWS credentials**: no `id-token: write`, no credential-configuration
step, unusable `AWS_*` values with IMDS disabled, and every `terraform init` uses
`-backend=false`. That last one is structural rather than stylistic — with the
`backend "s3"` block declared, a `plan` after a `-backend=false` init exits non-zero
with *Backend initialization required*, so CI cannot plan, apply or write state even
by accident.

Two things worth knowing if you are reading a green check on a PR here:

- Before this workflow existed, a fixture-edge-only PR triggered **`Gateway Infra
  Plan`** (it globs `modules/gateway/infra/**`), which authenticates to the account
  and plans the *ordinary* gateway. This root is not a module of that one, so that
  job never evaluated a line of this component while still reporting green. If you
  see only that check on a fixture-edge PR, this component was not tested.
- A green **`Fixture Edge CI`** proves configuration properties: default-off, the
  run-binding gate refusing mismatched discovered inputs, the Deny staying scoped to
  `/internal`, no output carrying the provenance secret, and the scripts refusing
  the unsafe orderings. It proves **nothing about live behaviour**. Steps 6 and 7
  are the only source of that.

`tests/test_ci_wiring.py` gates the workflow itself — that the trigger paths reach
this component and #3968's `lib/ownership.py`, that every suite in `tests/` is
actually invoked, and that no step can reach AWS.

## Boundaries

- **No ordinary rollout, flag change or platform apply** is part of this procedure.
- **No IAM change is required.** The worker role's existing grants already cover
  `/internal/*` with a wildcard API id, so supported access needed no widening;
  restriction lives on the fixture API's resource policy instead.
- The provenance secret must never be echoed, committed, or pasted into an issue
  comment or workflow log. Step 5 pipes it directly.
- The repository tests are mocked and prove configuration properties only. Live
  acceptance comes from steps 6 and 7.
