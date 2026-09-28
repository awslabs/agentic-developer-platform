# Account Factory (adopted)

AWS account and EKS provisioning for Superplane workspaces, via [kro](https://github.com/kubernetes-sigs/kro)
`ResourceGraphDefinition`s reconciled by [ACK](https://github.com/aws-controllers-k8s)
controllers on a management cluster.

Adopted into maintained ADP ownership by **issue #5530** (EPIC A #4910, Wave 6, `w6-07`) from
a read-only reference tree that U22 (#5326) deliberately did **not** transfer — see
[`../../src/TRANSFER-MANIFEST.md`](../../src/TRANSFER-MANIFEST.md), which lists
`infra/account-factory` under "Also **not** transferred". The reference was read as source
evidence and never built or deployed; selected reviewed implementation was copied here.

> **This module is offline. It cannot provision anything.**
>
> There is no code path from here to a subprocess, a socket, an AWS client or a Kubernetes
> client — enforced as a source-level property by
> `tests/test_no_legacy_targets.py::test_the_module_cannot_fetch_execute_or_mutate_anything`.
> It validates requests, renders the objects a request *would* apply, and plans cleanup.
> Applying any of that is a separate, separately-authorized operation.
>
> Neither the creation nor the merge of #5530 authorizes account vending, an AWS or Kubernetes
> apply, feature activation, image promotion or workload spend.

## Quick start

```bash
cd modules/domain-apps/superplane/infra/account-factory

python3 -m account_factory.cli dependencies                      # the verified pin set
python3 -m account_factory.cli validate     --config request.yaml
python3 -m account_factory.cli render       --config request.yaml
python3 -m account_factory.cli cleanup-plan --config request.yaml

# whether CreateAccount may be called at all, given what is recorded about a prior attempt
python3 -m account_factory.cli creation-status --config request.yaml \
  --attempt-ledger recorded-attempts.yaml

# what bootstrapping the child account requires, in the order it must be done
python3 -m account_factory.cli bootstrap-plan --config request.yaml

# the account was created but bootstrap failed: what exists, what a retry would repeat,
# what is retained. Steps you do not report are NOT-CHECKED, never "fine".
python3 -m account_factory.cli recovery-report --config request.yaml \
  --attempt-ledger recorded-attempts.yaml \
  --observed bootstrap-role=established \
  --observed autoscaling-service-linked-role=denied \
  --observed-detail autoscaling-service-linked-role="iam:CreateServiceLinkedRole denied"
```

Start from [`config.example.yaml`](config.example.yaml) — it documents all three modes with
placeholders only, and **deliberately does not validate**. A working default target is the
legacy defect this adoption removes, so the example is unusable until you supply real
identities.

There is no `apply` subcommand, by design. See *Plan and apply* below.

## Ownership modes

The issue asks for three modes, and for the three ownership questions — organization,
management cluster, workspace — to be distinguishable rather than conflated. Each mode is a
different answer to "what does ADP own here", and that answer drives what gets rendered *and*
what cleanup may delete.

| Mode | ADP creates | ADP owns | Renders | Cleanup deletes | Can close the account |
|------|-------------|----------|---------|-----------------|-----------------------|
| `new-account-managed` | account, VPC, cluster | account, VPC, cluster, workspace | `AccountOwnership` + `WorkspaceInfrastructure`, in two stages | the `WorkspaceInfrastructure` root — **retaining** `AccountOwnership`, which owns the account | only via an explicit `closure_request`, and only the account it recorded opening |
| `existing-account-managed` | VPC, cluster | VPC, cluster, workspace | `IAMRoleSelector` + `WorkspaceInfrastructure`, one stage | the `WorkspaceInfrastructure` root and the `IAMRoleSelector` | **never** — ADP did not open the account |
| `bring-existing-cluster` | nothing | the workspace only | `IAMRoleSelector` + an adoption `ConfigMap` | `IAMRoleSelector` and the workspace's `ConfigMap` | **never** |

Cleanup deletes only namespaced objects in the workspace's own namespace, and every plan reports
what it **retains** and why — so "this was not deleted" is visible in the plan rather than being
an absence a reviewer has to notice.

Cleanup deletes the **root** object rather than the stacks inside it, which is a consequence of
how reconciliation works rather than a preference: a root object *declares* the `NetworkStack`
and `EKSClusterStack`, so deleting those two while the root survives is not a deletion — it is a
request that the controller rebuild them. `cleanup._check_convergence` refuses any plan that
deletes a kind a retained root still declares, as a property check rather than a list of known
combinations.

That is also why `new-account-managed` renders **two** root objects instead of the vendored
`FullAccountInfrastructure`: see *Account ownership and infrastructure ownership are separate
objects* below.

Each mode requires exactly its own fields and **refuses the other modes' fields**. Refusing
matters as much as requiring: a `target_account_id` supplied with `new-account-managed` is not
harmlessly redundant — it means the caller believes the account exists while the request says
to create one, and one of those two beliefs is about to act on the wrong account. Likewise
`cluster_version` in `bring-existing-cluster` is refused rather than ignored, because a value
that cannot take effect is a value whose author was wrong about what the request does.

An unsupported mode fails **before any object is produced** (`modes.ensure_valid`, called first
by both `render` and `cleanup.plan`).

## Layout

| Path | What it is |
|------|------------|
| `account_factory/modes.py` | the three modes, request shape, and all validation (including whose workspace a run may act on) |
| `account_factory/dependencies.py` | reads and **verifies** the lock; refuses anything unpinned or drifted; exposes each graph's declared input schema |
| `account_factory/render.py` | the object set a request would apply, as ordered stages, + the shared prerequisite plan |
| `account_factory/cleanup.py` | workspace delete plans, ownership evidence, and the separate account-closure request |
| `account_factory/creation.py` | the durable creation-attempt record, the duplicate-account fence, and the retry decision (#5531) |
| `account_factory/bootstrap.py` | what child-account bootstrap must establish and in what order — the three scoped roles, the Auto Scaling service-linked role a workspace KMS key depends on, and the baseline controls (#5531) |
| `account_factory/recovery.py` | the create-succeeded/bootstrap-failed report — what exists, what is incomplete, what a retry would and would not repeat, what is retained; unchecked is never absent (#5531) |
| `account_factory/cli.py` | offline entry point; no apply subcommand |
| `dependencies.lock.yaml` | digest pins for 5 charts, checksums for 3 vendored graphs, `target: unresolved` |
| `vendor/kro-account-factory/` | the three upstream RGDs at a pinned commit, with the Apache-2.0 LICENSE — **read-only** |
| `manifests/` | ADP's own two RGDs: `adp-account-ownership.yaml`, `adp-workspace-infrastructure.yaml` |
| `config.example.yaml` | all three modes, placeholders only |
| `tests/` | 483 offline tests |

`vendor/` and `manifests/` are both resource graphs and are deliberately treated differently.
Vendored files are third-party, so the lock records a checksum and `dependencies.load` recomputes
it on every call — without that, `vendor/` is just files someone could edit. Maintained files are
ADP's own code, so they are **not** checksummed: a maintained file's review *is* its diff, and a
checksum a maintainer updates in the same commit as the edit records nothing. What the lock does
require of them is that the file exists and declares the kind claimed for it, so a rendered custom
resource can never point at a definition that is absent or renamed.

## What was deliberately changed in the adoption

The reference worked, but four properties made it unsafe to adopt as-is. Each is closed by
construction and pinned by a test, so the fix cannot silently regress.

Every legacy quote below is from `modules/domain-apps/ai-super-plane/reference/infra/account-factory/`
at commit `98ab544d52cdb96c84b724ea78a74ddb007dd864`. That tree is **not** on `main` — it was
reference evidence, never adopted — so a reviewer checking these quotes needs to fetch the commit
explicitly rather than look for the path in a checkout:

```bash
git fetch origin 98ab544d52cdb96c84b724ea78a74ddb007dd864
git show 98ab544d:modules/domain-apps/ai-super-plane/reference/infra/account-factory/config.env
```

### 1. Fixed targets are gone, and refused by value

`config.env` shipped working defaults: management account `605440105851` (not ADP's), cluster
`github-arc-runner-eks` (core ADP's ARC runner cluster), a named individual's email address as
the child-account address, and `superplane-test` as a fixed account name that would collide
across two independent requests. A run that supplied nothing still acted on a specific real
target.

Nothing is defaulted now — every identity is supplied per request — and the four legacy values
are additionally refused *by value*, case-insensitively, wherever they appear in a request.
Un-defaulting alone would leave them working if pasted back in.
`tests/test_no_legacy_targets.py` scans the module as committed and distinguishes a legacy
value **named in order to be refused** (legal, in the denylist and in prose) from one **used as
configuration** (refused).

### 2. Dependencies are pinned, and verified on every load

Two mechanisms meant the reference could not say what it deployed:

```bash
# 03-deploy-rgds.sh — a moving branch, and a reused /tmp directory
if [ ! -d "$KRO_EXAMPLES" ]; then
  git clone --depth 1 https://github.com/kubernetes-sigs/kro.git /tmp/kro
fi
kubectl apply -f "${KRO_EXAMPLES}/01-network-stack.yaml"

# 02-enable-eks-capabilities.sh — "latest" at deploy time, and fail-open
RELEASE_VERSION=$(curl -sL ".../releases/latest" | ... 2>/dev/null || echo "")
if [ -z "$RELEASE_VERSION" ]; then
  echo "   WARNING: Could not determine latest version for $SERVICE, skipping"
  continue
fi
```

The first applies whatever the default branch holds at clone time — and because it reuses
`/tmp/kro` when present, on a reused runner it applies whatever a *previous run* left on disk.
The second turns a transient network error into a partially-installed control plane reported as
success, which is a correctness bug rather than a style one.

Now: the graphs are **vendored** at commit `3b6e8c51` with their licence, and every chart is
pinned by **content digest** (a version string is a label someone can move). `dependencies.load`
recomputes the vendored checksums on every call and raises if anything drifted — without that,
`vendor/` is just files someone could edit and the lock's checksums would be decoration.
**Refusal is total**: there is no partial-success path, because that path is the defect.

### 3. Shared prerequisites are explicit operations, not side effects

`deploy.sh` ran four steps in sequence, and the first two changed the shared cluster before the
fourth provisioned an account. `01-setup-iam-roles.sh` minted IAM roles carrying
`AWSOrganizationsFullAccess`, `IAMFullAccess` and `AmazonEC2FullAccess`, plus a
`CrossAccountAssumeRole` inline policy on `arn:aws:iam::*:role/OrganizationAccountAccessRole` —
a wildcard account, so the role could assume into *any* account in the organization rather than
the one being requested. `02-enable-eks-capabilities.sh` then installed the cluster-wide kro and
ACK controllers. Asking for one workspace changed the shared control plane.

`render` therefore returns two separate things:

* **`objects`** — the namespaced objects for this workspace. Applying them provisions one
  workspace and changes nothing shared.
* **`prerequisites`** — the shared installs, as a reviewed list with digest-pinned references,
  for an operator to run deliberately **once per management cluster**.

Rendering executes none of them and never merges them into `objects`. The list is
mode-dependent: `bring-existing-cluster` does not ask for the Organizations, EC2 or EKS
controllers it has no use for.

### 4. Plan and apply are separate, and closure is never implicit

The reference had no reviewable intermediate — `echo "$MANIFEST" | kubectl apply -f -` built the
manifest and mutated the cluster in one statement, so the only way to see what it would do was
to let it do it. `render` *is* that intermediate. There is no `apply` here at all, which is what
makes the boundary real rather than advisory.

Teardown was worse. `06-teardown-account.sh` ran a single
`kubectl delete fullaccountinfrastructure <name>`; because the vendored graph owns the `Account`
resource, that one command cascaded into deleting the AWS account.

To its credit the script disclosed this upfront — `The AWS account itself will enter a 90-day
suspended state` — and prompted for a typed confirmation. But the prompt was guarded by
`if [ -t 0 ]`, so under any automation the warning printed into a log nobody was reading and the
delete proceeded unconfirmed. The structural problem the guard cannot fix is that an irreversible
consequence was reachable by deleting a custom resource named after a *workspace*: the object a
caller reaches for to decommission a workspace was the object that closed the account.

Now a workspace plan deletes the `WorkspaceInfrastructure` root and **retains** the
`AccountOwnership` that owns the account, naming the retention. That split is what makes the
retention meaningful rather than nominal — see the next section. Closing an account is
`cleanup.closure_request`, which refuses an adopted account, an unacknowledged irreversible
consequence, a missing or malformed account id, a missing reason, and any account other than the
one a `ProvisionedAccountRecord` says this module opened for this workspace. `CleanupPlan` exposes
no route to it.

`07-teardown-capabilities.sh` also deleted shared CRDs, uninstalled the shared kro and ACK
controllers, deleted shared IAM roles and removed the EKS access entry for core ADP's
`github-runner-org` ARC runner role — on the `github-arc-runner-eks` cluster from `config.env`,
so tearing down *the account factory's* capabilities revoked the ARC runners' access to a cluster
they, not it, depended on. Nearly every destructive step ended in `2>/dev/null || true` or
`|| echo "Failed to delete"`, so a failure that left shared IAM roles behind still exited 0 —
`set -euo pipefail` at the top notwithstanding, since the suppression defeats it.

`DeleteAction` has no cluster-scoped
variant (a type that *cannot* express a cluster-wide delete beats a check that rejects one), and
the guard **refuses** — never skips with a warning — shared cluster kinds, core ADP namespaces
and other workspaces' namespaces. Resources discovered from live cluster state go through the
same guard, rather than being trusted because they were found.

### 5. Creating an account is a named mode, and asking twice cannot open two (#5531)

`CreateAccount` does not return an account. It returns a `CreateAccountStatus` id, and the
outcome arrives later. The reference applied an `Account` resource and moved on, so there was
nowhere recording that an attempt had been made and no way to ask what became of it.

The expensive case is a **lost answer**: the reply never arrives, the process handling it dies,
or a throttle response hides whether the call landed. The only recovery move available is to ask
again — and asking again when the first attempt in fact succeeded opens a **second AWS account**.
Both are real and both cost money. Removing the spare is not a cleanup; it is the same
irreversible 90-day suspension, of an account whose id cannot be reused in that window.

`creation.py` provides offline classification. `may_create_account` is always false,
and `creation-status` always exits nonzero with no executable pre-call record, even
when caller-supplied authorization flags are complete. Its legacy disposition names
classify evidence; they grant no authority. The maintained account-provisioning runner
loads trusted durable history, checks complete admitted-operation authorization, and
commits a new generation before every permitted first call or retry.

The offline ledger searches immutable organization/workspace identity before comparing
the approved payload. Changing email or OU cannot hide an unresolved attempt. The
runtime fence additionally binds the operation and generation. Cluster-input changes
remain distinct from account-identity changes.

New-account rendering requires an operation-bound `CreatedAccountRegistration` from
`account_provisioning.registration.load_created_account`. A free `--account-id` or
`account_id` argument is refused. Existing-account onboarding continues to use the
explicitly authorized request target.

**Unknown is not failure.** This is the distinction the module turns on, the same one
`contracts/superplane_contracts/reconciliation.py` draws: "the provider says this did not happen"
and "I could not find out what happened" are different facts with opposite safe actions. A
confirmed failure created nothing, so repeating is safe. An unreadable outcome means an account
may exist that nobody is tracking — repeating is the duplicate-spend bug, and concluding failure
is the leak. `UNRESOLVED` therefore authorizes *nothing*, and there is no `--aws-status unknown`
flag, because "I could not check" is not something AWS reported.

Failure reasons are enumerated rather than free text because two of them must never be retried
with the same input: a taken root-user address fails identically forever, and an exhausted
account quota needs a human, not a backoff. Both surface as
`REFUSED_INPUT_CANNOT_SUCCEED` — and a taken address is reported rather than worked around, since
it may belong to an account in another organization entirely.

Placement is part of creation, not a follow-up. `organizational_unit_id` is **required** in
`new-account-managed` and has no default, because omitting a parent does not mean "no OU" to
AWS — it places the account at the organization root, the least restricted placement available.
The rendered `Account` carries `parentIDs` so the account lands inside its guardrails at
creation, with no window in which it is live outside them. In `existing-account-managed` the same
field is **refused**: acting on it would re-parent an account that already exists as a side
effect of a workspace request, and ignoring it would mislead whoever wrote it.

### 6. A vended account is bootstrapped before a workspace touches it (#5531)

A newly created AWS account is not usable. It has no identity ADP can assume, no baseline
controls, and — the specific gap #5532's review surfaced — no `AWSServiceRoleForAutoScaling`.

That last one is not cosmetic. The workspace side creates a KMS key whose **key policy names
that role's ARN**, and KMS validates every principal in a key policy at key-**creation** time.
If the role does not exist, the key cannot be created at all, and the error reads as a
malformed policy about a principal rather than as a missing account-wide role. So
`workspaces/scripts/workspace_kms.py::verify_account_prerequisites` does a read-only
`iam get-role` and fails closed with remediation naming bootstrap as the owner:
*"workspace provisioning never creates or adopts this account-wide role."* `bootstrap.py` is
the other half of that contract, and `bootstrap-plan` orders the role **before** anything that
would create a key.

The role belongs to the account, not to a workspace, and that boundary is the point. A
workspace that took it into its own Terraform state would delete it on teardown — and every
*other* workspace in that account would then fail its next encrypted-node operation. One
workspace's cleanup breaking its neighbours is a hard failure to diagnose, so
`adoptable_by_workspace` is `False` and `retained_through_workspace_retirement` is `True` for
every step in the plan, and claiming both at once is refused at construction.

Idempotency is by **reading first**, not by swallowing errors. `PresenceRule.CREATE_IF_ABSENT`
means create only after a read has *verified* absence; a create that ignores an
already-exists error cannot distinguish "it was already there" from "the create was denied",
and those need opposite responses. Existing roles are reused exactly, and
`iam:CreateServiceLinkedRole` is remediated as **scoped to `autoscaling.amazonaws.com`** —
unscoped, it would let bootstrap mint a service-linked role for any AWS service in the account.

Bootstrap establishes **three** scoped roles rather than one. A single role would hold the
union of all three tiers' permissions, so the workload — the least trusted, running arbitrary
tenant work — would inherit the ability to create IAM roles and read the account's baseline
controls. Only `RoleTier.BOOTSTRAP` may write IAM, which makes "the workload cannot
re-bootstrap the account" a property of the credential rather than of a review.

The ordering is **checked**, not merely arranged. `check_order` refuses a plan that omits the
service-linked role, omits the bootstrap identity, or orders either the service-linked role or
a lesser role before the identity that creates it — because the steps all look reasonable in
any order, and a property maintained only by the order someone happened to write a tuple in is
one a later edit silently breaks.

`bring-existing-cluster` gets **no** plan: it adopts a cluster someone else runs, so
"bootstrapping" it would rewrite roles and baseline controls in an account ADP does not own.
Both account-owning modes do get one — skipping it for an adopted account is precisely what
leaves the service-linked role unchecked until a KMS key creation fails confusingly.

### 7. Create-succeeded/bootstrap-failed is a report, not a status (#5531)

The worst state this module has to handle is the half-built one: the AWS account is real and
billable, and the roles, service-linked role or baseline controls that make it usable are partly
or wholly absent. Both obvious moves are wrong. **Retry from the top** can open a second account
if the creation outcome was never read. **Tear it down and start over** is not a reset — closure
is the same irreversible 90-day suspension of an id nobody can reuse in that window, so it costs
a permanent account *and* buys a new one.

A "failed" status collapses four questions that have different answers, so `recovery.py` returns
a report instead: what **exists**, what is **incomplete**, what a retry would and would not
**repeat**, and what is **retained** regardless. `recovery-report` emits all four, plus the
`next_action` each step's own state implies.

**Unchecked is not absent.** This is `creation.py`'s unknown-≠-failure distinction applied to
bootstrap, and it is why `StepState` has four members rather than a boolean. "The role is not
there" invites creating it; "I did not look" invites looking. So `NOT_CHECKED` is what a step a
caller did not report **defaults to** — a caller cannot shrink the report by supplying fewer
observations — it keeps `every_step_accounted_for` false, and it makes `bootstrap_retry_is_safe`
false. `DENIED` is separate from `ABSENT` for the same reason in the other direction: retrying
over a denial changes nothing until the permission is granted, which is an infinite loop that
looks like progress. A `DENIED` finding with no detail is refused at construction, because a
denial nobody described cannot be told from an assumption, and the remedy depends on *which*
permission was refused.

Three further properties are deliberate:

* **`creation_retry_is_safe` is a constant `False`**, not an omission, because the question gets
  asked. Bootstrap recovery never re-runs `CreateAccount`; whether creating is permitted at all
  is `creation.assess_attempt`'s answer from the recorded ledger.
* **`ready_for_workspace_provisioning` requires a recorded account, not just clean steps.**
  Observations can report every step established while no recorded attempt says this workspace
  has an account — meaning the observations are about some *other* account, or the attempt was
  never recorded. Neither is a state to build a workspace in, so a clean step list is not
  allowed to stand in for a real account, and the summary says `NO RECORDED ACCOUNT` rather than
  `COMPLETE`. The account id comes from the creation decision's recorded attempt and is not a
  caller argument at all.
* **`blocking_prerequisites` keys on the step's own `blocks_workspace_provisioning` flag**, not
  on whether it `precedes` something. Every step precedes something, so that test would call the
  whole plan blocking and tell the reader nothing. The distinction it has to carry is real: a
  missing baseline control is a gap to close, while a missing service-linked role means the next
  workspace KMS key creation *fails*.

## Account ownership and infrastructure ownership are separate objects

The vendored `FullAccountInfrastructure` declares the AWS account *and* the VPC *and* the cluster
in one object. That single-object ownership makes teardown unsafe in **both** directions, and no
choice about which object to delete fixes it:

* delete it, and removing a workspace's infrastructure also closes the AWS account into an
  irreversible 90-day suspension;
* retain it, and the VPC and cluster *declaration* is retained too — so a controller reconciling
  the surviving root rebuilds the infrastructure that was just deleted. The teardown reports
  success and the spend comes back.

So `new-account-managed` instantiates two independent roots from ADP's own maintained graphs
instead:

| Root | Declares | Lifecycle |
|------|----------|-----------|
| `AccountOwnership` (`manifests/adp-account-ownership.yaml`) | the Organizations `Account` and the `IAMRoleSelector` reaching into it | retained by a workspace teardown; removable only through `closure_request` |
| `WorkspaceInfrastructure` (`manifests/adp-workspace-infrastructure.yaml`) | the `NetworkStack` and the `EKSClusterStack`, wired together | deleted by a workspace teardown, and stays deleted |

Nothing retained declares the VPC or the cluster, and nothing deleted declares the account, so the
plan converges.

### The cluster is wired to the network inside one graph

`WorkspaceInfrastructure` holds both stacks rather than being two rendered objects because the
cluster's required `subnetIds` and `securityGroupIds` are **outputs of the network**. Two
independent top-level objects cannot carry one's output into the other's input, so rendering them
side by side produced an `EKSClusterStack` with both required fields absent — a plan that is either
rejected or leaves a VPC and IAM roles behind with no cluster. Inside one graph, kro's
`${network.status.privateSubnetIds}` reference supplies the value *and* orders the two.

`render._check_graph_inputs` generalises that: every rendered custom resource is compared against
its own graph's declared schema, in both directions. A missing required input is refused (the
object cannot reconcile), and an **unknown** input is refused too — kro silently ignores an
undeclared field, so a misspelled `subnetIds` would otherwise look exactly like the original
defect wearing a new disguise. Optional inputs, which kro marks with `default=`, may be absent;
that is what the marker means.

### Ordering across two roots is a stage, not an assumption

Splitting the roots costs something: kro orders resources by references *within* one graph and
cannot order two separate root objects at all. `new-account-managed` genuinely has that ordering
requirement — the VPC must be created in an account that exists — so `render` returns `stages`
with the requirement written down as a `precondition` rather than left implicit:

```
stage 0  workspace namespace
stage 1  account ownership
stage 2  workspace infrastructure
         precondition: stage 1 reports Ready with a non-empty status.accountId, and that id is
                       recorded as the provisioned account for this workspace
```

An explicit precondition is also what makes a run **resumable**: an operator applies a stage,
waits for the stated condition, and applies the next — and re-applying a stage whose condition
already holds continues a partial run instead of restarting it. The other two modes need one
stage each, because they wait on no other object's status.

## Who a run may act on, and what it may delete

Two checks answer questions that the earlier code did not ask at all.

**Which tenant.** `ValidationAuthorization` compares `workspace_id` and the permitted target
accounts, not only the organization and management cluster. The workspace decides the namespace
rendered into and the objects a cleanup plan deletes, so a run authorized for one workspace acting
on another is a cross-tenant operation — and before this comparison existed, a request naming a
different workspace with every other authorization field supplied was reported as fully verified.

That value is **authority-owned**: `ValidationAuthorization.from_operation_binding` takes it from a
binding's resolved principal, following the rule
[`contracts/superplane_contracts/provisioning.py`](../../contracts/superplane_contracts/provisioning.py)
states — the workspace an operation acts on comes from the binding's principal, so a caller cannot
name the tenant it provisions for. The CLI's `--authorized-workspace` exists because an offline
operator tool has no facade to resolve a principal from; it is named distinctly from the request's
own `workspace_id` so that supplying it is a statement about authority rather than a restatement of
the request. Omitting it is reported as a comparison **not made**, never as one that passed.

**Which resources.** A resource discovered on the cluster is guarded by more than its location:

* its kind must be one this module creates (`cleanup._OWNED_KINDS` is an **allowlist** — a denylist
  can only exclude what someone thought of, and the previous guard accepted any application
  `Secret`, `Deployment` or PVC that happened to share the namespace);
* `Namespace` is deliberately *not* in that allowlist: deleting it would destroy every object
  inside, including resources this module never created, while bypassing every per-resource check
  by never examining them;
* it must carry ADP's `managed-by` label and **this** workspace's label;
* its live `metadata.uid` must equal the uid recorded at provisioning. Labels are mutable, so a
  label match alone can be manufactured; a uid is assigned by the API server and never changes.
  This also refuses a resource that was deleted and recreated under the same name — the same
  identity on paper, a different object.

**Which account may be closed.** `closure_request` requires a `ProvisionedAccountRecord` — a
durable record that this module opened one specific account for this workspace and organization —
and refuses any id that does not match it. Checking only that an id is non-empty and
twelve digits accepts every account number in existence, including other teams' and other
organizations'; for the least reversible action in this module the default must be refusal. A
missing record is refused rather than treated as permission.

## Credentials and secrets

This module renders **identity references, never credentials**. Cross-account access uses an
`IAMRoleSelector`, which names a role ARN. No `Secret` is ever rendered, and `render` refuses
output containing anything secret-shaped.

Refusal messages name the *shape* and the *object*, never the value — a message that quotes
what it refuses becomes the disclosure it existed to prevent, since rendered output and error
text are both reviewed, logged and committed to CI artifacts. (An earlier version of this code
echoed the first 24 characters of the match; a test caught it.)

Credentials for a live operation come from the executing environment's role, never from
configuration.

## Verification

```bash
# the module's own suite (483 tests, offline, no credentials)
python3 -m pytest modules/domain-apps/superplane/infra/account-factory/tests/ -v

# the required check this module must not break: "Superplane domain tests"
python3 -m pytest modules/domain-apps/superplane/ -m "not superplane_live"

# lint, with the toolchain CI pins (ruff==0.9.6, modules/gateway/pyproject.toml)
ruff check modules/domain-apps/superplane/
ruff format --check modules/domain-apps/superplane/
```

Coverage maps to the issue's acceptance criteria:

| Tests | Establishes |
|-------|-------------|
| `test_modes.py` | unknown mode, wrong management account/organization, and wrong **workspace** or target account, refused before mutation; authority-resolved authorization |
| `test_dependencies.py` | unpinned or drifted dependencies refused; refusal is total; vendored and maintained graphs distinguishable; every graph exposes its required/optional inputs |
| `test_cleanup.py` | cleanup outside owned resources refused; ownership evidence required for discovered resources; every plan converges; closure never implicit and only for the recorded account |
| `test_render.py` | no legacy target, secret literal, or core-namespace write in rendered output; every object satisfies its graph's declared inputs; the cluster is wired to its network; stages carry their preconditions |
| `test_no_legacy_targets.py` | the module as committed carries no usable legacy target and cannot fetch or execute |
| `test_creation.py` | the attempt is recorded before the call; a replay of the same request is fenced as a duplicate; a retry requires a read prior status; an unreadable outcome is `UNRESOLVED`, never failure; failures a retry cannot fix are separated from ones it can; the persisted record round-trips and a mismatched or ambiguous store is refused |
| `test_bootstrap.py` | the Auto Scaling service-linked role is established before any workspace KMS key, using the bootstrap identity and never before it; an existing role is reused after a verified read rather than recreated; a denial fails closed with scoped remediation that does not hand the role to workspace provisioning; no step is adoptable into per-workspace state; the ordering is refused when violated rather than merely arranged; a plan states which authorization comparisons were not made; `bring-existing-cluster` gets no plan |
| `test_recovery.py` | a step nobody read is neither absent nor established, and defaults to `NOT_CHECKED`; a retry is never reported safe while the creation outcome is unresolved, a step is denied, or a step is unread; a retry acts only on verified-absent steps; a denial with no detail is refused; clean steps with no recorded account are not `COMPLETE`; the report covers the whole plan and refuses observations — or details — about steps outside it; what is retained is stated, and no closure is ever produced |
| `test_cli.py` | no apply path exists; refusals exit non-zero; unverified ≠ verified; `creation-status` exits non-zero on a duplicate or unresolved outcome, and `unknown` cannot be spelled as an AWS status; `bootstrap-plan` emits the ordering and the account-wide boundary, and refuses an unauthorized workspace or an account ADP does not own; `recovery-report` defaults unreported steps to not-checked, refuses an unparsable state instead of defaulting it, and exits non-zero on an unresolved outcome or an account not ready; both bootstrap subcommands report unmade authorization comparisons on stderr |

### What this evidence does not establish

Offline tests cannot close live criteria, and this suite does not claim to. Nothing here shows
that a chart installs, that kro reconciles a rendered graph, that an account can be vended, or
that the recorded digests are what the registries serve today. A digest records **which**
artifact a reviewed install would use.

The creation tests are the sharpest case of this. They establish the **decision rule** — given
what is recorded about a prior attempt, whether calling `CreateAccount` is safe — using AWS
answers the tests construct. No AWS Organizations call is made, so nothing here is evidence that
a real `CreateAccount` behaves as modelled, that a created account lands in the intended OU, or
that any account was ever opened or closed. Live account creation requires separate named
authorization and remains open.

The bootstrap tests are a **description** under test, not an account. They establish which steps
`bootstrap.py` names, in what order, with which presence rule and which remediation text.
Nothing calls AWS, creates or reads a role, or bootstraps anything: no role exists as a result
of running the suite or the `bootstrap-plan` subcommand. That a live account ends up with
`AWSServiceRoleForAutoScaling`, that `iam:CreateServiceLinkedRole` is in fact scoped on a live
identity, and that a workspace KMS key creation then succeeds are live criteria that separately
named authorization has to cover.

The recovery report is **arithmetic on observations somebody else made**. It reads nothing: every
`StepState` it reasons about is supplied by whoever actually looked, and `recovery-report` verifies
none of them. So the suite establishes that a correct set of observations yields a correct verdict —
and, more importantly, that an *incomplete* set cannot yield a clean one — but it is not evidence
about the state of any account. A report saying `COMPLETE` is only as true as the reads behind it,
and no read happens offline. Recovery also never repairs anything: `next_action` is text for an
operator, and no subcommand can carry it out.

Two things are also genuinely unresolved rather than merely untested:

* **The live target** — organization, management account, management cluster and spend limits.
  `dependencies.lock.yaml` records this as `target: status: unresolved`; it is the EPIC A
  supervisor's to settle before any live operation. A placeholder would look like an answer.
* **Authorization comparisons that were not made.** `ValidationAuthorization` fields are
  optional so the validator runs offline, but an absent field means the comparison **was not
  performed**, and that is reported explicitly (`unchecked`, and `NOT VERIFIED` on stderr)
  rather than counted as a pass. "The management account was not verified" and "the management
  account matched" must not look alike in a report. `BootstrapPlan` carries the same list, so
  `bootstrap-plan` and `recovery-report` report it too — those two describe writing
  account-wide roles and can call an account ready for a workspace, which makes an unverified
  run reading like a verified one worst there.

## Upstream provenance

| | |
|---|---|
| Repository | `https://github.com/kubernetes-sigs/kro` |
| Revision | `3b6e8c5170f5df1a9ffa9411170ae443f7da722d` (a commit, not a branch) |
| Path | `examples/aws/aws-accounts-factory` |
| Licence | Apache-2.0, vendored at `vendor/kro-account-factory/LICENSE` |

Files under `vendor/` are third-party and **must not be edited** — `dependencies.load` verifies
their checksums, so a local edit fails the suite. To change them, re-vendor from a pinned
revision and update the lock in a reviewed commit.

`vendor/03-full-account-infrastructure.yaml` is kept as vendored provenance and is deliberately
**not instantiated** by any mode; `manifests/` holds the two graphs rendering actually uses. The
reason is in *Account ownership and infrastructure ownership are separate objects* above, and
`cleanup._DECLARED_BY` records what that graph declares so that instantiating it in future cannot
quietly reintroduce the non-convergent teardown.

Two constraints recorded by the reference are kept because they determine which install path is
usable: the kro **EKS Capability** build (v0.8.4) was recorded as not reconciling
`ResourceGraphDefinition`s reliably, so the pinned chart is the self-managed install; and AWS
Organizations is **not** included in the ACK EKS Capability, so the controller that creates
accounts must be installed separately regardless of install path.
