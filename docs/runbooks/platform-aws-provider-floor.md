# Runbook: The Platform AWS Provider Floor and the State-Schema Migration

Covers why `platform/infra` requires AWS provider >= 6.42.0, how the failures it
fixes present, how to reproduce them without touching a live account, and — the
part an operator actually needs before a scoped rollout — **the order the
provider upgrade and the state-schema migration must happen in.**

**Issue:** #5831 · **Parent epic:** #3959 · **Deployment prerequisite for:** #5830

> **If you are here to run a scoped rollout, read [Section 5](#section-5--the-supported-rollout-sequence) first.**
> Raising the provider floor is necessary but **not sufficient**: a targeted plan
> against mixed-age state still cannot be exported for review until the state
> schema is migrated. Section 5 is the supported sequence. It does **not** include
> an ordinary full apply, which against this source would propose destroying the
> CoreDNS add-on.

---

## Section 1 — What was wrong, and why it was invisible

There are **two** independent incompatibilities in the real state, not one, and
they pull in opposite directions. This section covers the first; Section 5
covers the second and the sequence that resolves both. The first attempt at
#5831 fixed only this one and reported the blocker cleared — it was not, because
a fixture carrying only the newer record passes while the real path still fails.

### 1a — The newer record: no identity schema (fixed by the floor)

A saved Terraform plan could not be exported for review. The platform module
resolved AWS provider **5.100.0** — the final 5.x release, which is what
`~> 5.0` resolves to, making that constraint effectively a pin — while the dev
account's state record had been written by a newer provider.

The decisive detail is narrower than general version skew. Provider 5.x
publishes **no resource *identity* schema** for `aws_eks_addon`. Terraform's
JSON output includes each resource's identity, so once the state record carried
the identity fields a newer provider writes — `account_id`, `addon_name`,
`cluster_name`, `region` — Terraform could no longer serialise that state:

```
Failed to marshal plan to json: error marshaling prior state:
no resource identity schema found for aws_eks_addon.coredns
  (in provider registry.terraform.io/hashicorp/aws)
```

**This failure mode is quiet in the worst way.** A plain `terraform plan` still
reported `No changes.` — it only warned `Failed to decode resource from state
... unsupported attribute "namespace_config"`. Nothing looked wrong until
`terraform show -json <saved-plan>`, the step that makes a saved plan
*inspectable*, failed outright. So the blocker was the review step, not
planning; and the only ways to "proceed" without fixing it would have been to
apply without inspecting the plan, or to edit state to remove the fields the
old provider disliked. Both were explicitly out of bounds, and both would have
destroyed the audit property the saved-plan review exists to provide.

## Section 2 — Why 6.42.0 specifically

`namespace_config`, the field present in the state record, was added to
`aws_eks_addon` in **AWS provider 6.42.0**. That makes 6.42.0 the floor, and it
is why a bump to merely "6.x" would not have been reliably sufficient.

The boundary was verified empirically rather than inferred from release notes,
using Terraform 1.14.9 against a representative newer-provider state record:

| Provider | `terraform show -json` on the saved plan |
|---|---|
| 5.100.0 (`~> 5.0`) | fails — `no resource identity schema found` |
| 6.41.0 | fails — same message |
| 6.42.0 | succeeds — state decodes, JSON exports |

The constraint is `>= 6.42.0, < 7.0.0`. The upper bound is deliberate: a
provider major bump carries its own breaking changes and needs the same
state-decoding review, so v7 must not arrive on the next `init` unreviewed.

**Do not lower this floor.** Doing so reintroduces the exact blocker.

## Section 3 — Reproducing and verifying without a live account

Everything below is credential-free and touches no AWS account. It uses a
synthetic state record, not the real one.

Create a working directory with a minimal configuration and a state record
shaped like the one that triggered the failure — an `aws_eks_addon` carrying
`namespace_config` and the four identity fields:

```bash
mkdir -p /tmp/floor-check && cd /tmp/floor-check

cat > main.tf <<'TF'
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 5.100.0" }
  }
}
provider "aws" { region = "us-east-1" }
resource "aws_eks_cluster" "main" {
  name     = "adp-dev"
  role_arn = "arn:aws:iam::000000000000:role/placeholder"
  vpc_config { subnet_ids = ["subnet-aaaa", "subnet-bbbb"] }
}
TF

cat > terraform.tfstate <<'JSON'
{
  "version": 4, "terraform_version": "1.14.6", "serial": 84,
  "lineage": "00000000-0000-0000-0000-000000000000",
  "outputs": {}, "check_results": null,
  "resources": [{
    "mode": "managed", "type": "aws_eks_addon", "name": "coredns",
    "provider": "provider[\"registry.terraform.io/hashicorp/aws\"]",
    "instances": [{
      "schema_version": 0,
      "attributes": {
        "addon_name": "coredns", "cluster_name": "adp-dev",
        "id": "adp-dev:coredns",
        "namespace_config": [{"namespace": "kube-system"}],
        "tags": {}, "tags_all": {}
      },
      "identity_schema_version": 0,
      "identity": {
        "account_id": "000000000000", "addon_name": "coredns",
        "cluster_name": "adp-dev", "region": "us-east-1"
      },
      "sensitive_attributes": []
    }]
  }]
}
JSON
```

Reproduce the failure on the old provider. Note the plan itself succeeds — only
the JSON export fails, which is the point:

```bash
terraform init -input=false
terraform plan -refresh=false -target=aws_eks_cluster.main -out=tf.plan
terraform show -json tf.plan    # expected: "no resource identity schema found"
```

Confirm the fix by raising only the constraint, then **re-planning** before the
export:

```bash
sed -i 's/= 5.100.0/>= 6.42.0, < 7.0.0/' main.tf
rm -rf .terraform .terraform.lock.hcl
terraform init -input=false
terraform plan -refresh=false -target=aws_eks_cluster.main -out=tf.plan
terraform show -json tf.plan | head -c 200    # expected: JSON, format_version 1.2
```

The re-plan is required, not incidental. **A saved plan file is tied to the
provider version that produced it**, so a plan saved under 5.100.0 cannot be
exported after the upgrade — it fails with a different error that is easy to
mistake for the original bug:

```
error in marshalPlannedValues: error decoding 'after' value:
an object with 34 attributes is required (28 given)
```

If you see that message, the plan file is stale; regenerate it rather than
concluding the floor is wrong. Any previously saved plan from before the upgrade
must be discarded and re-created.

State can also be exported directly, which is the narrower check that the record
itself decodes:

```bash
terraform show -json | head -c 200            # expected: JSON, format_version 1.0
```

`-refresh=false` keeps this offline; no credentials are needed or used.

## Section 4 — What the upgrade changed in this repository

**One test needed correcting.** Provider 6.x represents
`aws_bedrock_model_invocation_logging_configuration`'s `logging_config` — and
its nested `s3_config`, `cloudwatch_config` and
`large_data_delivery_s3_config` — as **lists of objects**. Under 5.x the test
read them as bare attributes, which fails on 6.x with `Block type
'logging_config' is represented by a list of objects, so it must be indexed
using a numeric key`. The traversal in
`platform/infra/modules/bedrock-invocation-logging/tests/logging.tftest.hcl`
is now indexed (`logging_config[0]`). The asserted wiring is unchanged. This was
a genuine upgrade-caused regression — the suite passes on 5.x — not
pre-existing debt.

**Module suites, verified on the new provider:** EKS 9/9 (run through the
`-plugin-dir=../../.terraform/providers` shape `platform-infra-plan.yml` uses,
so it exercises the provider the root constraint actually resolves), container
registry 2/2, Bedrock invocation logging 5/5.

**A known, deliberately deferred warning.** Provider 6.x deprecates
`data.aws_region.current.name` in favour of `.region`. 14 call sites across
`platform/infra` emit this warning. They are **warnings, not errors** — the
configuration validates and plans. Rewriting them was left out of #5831 to keep
the fix scoped to the blocker. One carries real risk when it is addressed:
`modules/bedrock-invocation-logging/main.tf` feeds the region into an **S3
bucket name**, so a changed value there would force bucket replacement. Verify
the resolved value is identical before changing those call sites.

## Section 5 — The supported rollout sequence

### 5a — The second defect: a stale record the floor cannot fix

With the floor raised, a **freshly generated** targeted plan against the real dev
state still fails to export — on a different resource, with a different message:

```
Failed to marshal plan to json: error marshaling prior state:
schema version 0 for aws_launch_template.gvisor_nodes in state does not match
version 1 from the provider
```

This is not a stale plan file (Section 3's trap) and not the Section 1 defect.
It is the opposite problem: a record **older** than the provider's schema.

**The mechanism, which explains everything else in this section.** Terraform
upgrades a resource's state schema only for resources **in scope for the run**.
`-target` puts everything else out of scope, so untargeted records keep their
recorded `schema_version` — while `terraform show -json` serialises *all* prior
state, not just the targeted subset. **Targeting is what turns a stale record
into an export failure.** A full-scope plan over the same state exports fine,
because every resource is in scope and gets upgraded in memory.

**No provider version fixes both defects.** This is why the answer is a state
migration and not a different version bound:

| Provider | `aws_launch_template` schema | `aws_eks_addon` identity schema |
|---|---|---|
| 5.100.0 | 0 — matches the old record | **absent** — breaks the add-on export |
| 6.0.0 – 6.14.0 | 0 — matches the old record | absent |
| 6.16.0 + | **1** — mismatches the old record | present from 6.42.0 |

Reading the old launch-template record needs schema 0; reading the add-on
identity needs 6.42.0+, which ships schema 1. **The requirements are disjoint.**
Read from `terraform providers schema -json`, not from release notes. The table is
recorded **observed research** across those versions. What a test can assert is
narrower — only the provider a run actually initialises is inspectable — so
`test_resolved_provider_cannot_satisfy_both_records` asserts that the *resolved*
provider cannot decode both records, and the cross-version rows above stand as the
recorded observation behind choosing a migration over a different bound.

A local state write (`terraform state mv`, for instance) does **not** upgrade the
schema — verified. The upgrade is persisted only by an apply whose scope includes
the resource. Hence the sequence below.

### 5b — The sequence

Each step is read-only until step 5, which is **root-operated** and writes state
only. Do not compress these steps; the ordering is what keeps the migration
reviewable.

**Two rules that apply to every step below.**

*Use the retained inputs, never Terraform's defaults.* `environments/dev/platform.tfvars`
deliberately leaves account-specific values unassigned so the repository stays
portable, and `platform-infra-apply.yml` supplies them at run time. A bare
`terraform plan` therefore does **not** plan the deployment you reviewed — it
plans a different one, with defaults substituted for the retained values. Every
plan command below passes the same `-var-file` arguments and the same prepared
inputs. `platform/scripts/upgrade-state.py prepare` is the canonical producer of
those inputs: it reads the live deployment and writes the retained values that
must not be lost.

*Keep state and plans in a private directory.* Both carry resource attribute
values, including secrets. `upgrade-state.py prepare` creates its task directory
mode `700` and its files mode `600`; put the snapshot and the plans in that same
directory. Do not use a predictable world-readable path such as
`/tmp/migrate.tfplan` — on a shared runner any local user can read it.

**A note on the shell — read this before running anything.** Every check below must
**stop the sequence** when it fails, and two plausible ways of writing that do not.

`|| return 1` inside a function exits *that function only*; it does not stop the
caller. And `step || echo "STOP"` does not stop anything at all — `||` *handles* the
failure, so the compound command succeeds and `set -e` has nothing to act on:

```console
$ bash -c 'set -euo pipefail; f() { return 1; }; f || echo STOP; echo WOULD_APPLY'
STOP
WOULD_APPLY        # <-- reached, and the script exits 0
```

So each step below is invoked with `|| exit 1`, and the whole sequence must run in a
**dedicated shell** — a script file, or `bash <<'EOF' ... EOF` — never pasted at your
own interactive prompt, where `exit` would close your session and where a typo in one
line still lets the next run.

**Steps 0–4 below only DEFINE functions. None of them runs anything.** That is
deliberate and load-bearing: a definition block that also invoked its own function
would run it while the later functions are still being collected, so `resolve_backend`
would execute before `confirm_account` had ever been called. Read steps 0–4 as one
script to assemble, then run the chain in **step 5**, which is the only place anything
executes. Start the script with `set -euo pipefail`.

**0. Establish the task directory and confirm the account.** Everything keys off
the account the active profile resolves, so confirm it before touching the backend
rather than after. A migration reviewed against one account and applied to another
is not a reviewed migration.

```bash
export AWS_PROFILE=embark1          # the mapped profile for the target account
export ADP_ACCOUNT=879318057152     # the intended target
export ADP_ENV=dev
export TASK_DIR="$HOME/.adp/migrate-5831"
export REPO_ROOT="$(git rev-parse --show-toplevel)"
export STATE_BUCKET="adp-terraform-state-${ADP_ACCOUNT}"
export STATE_KEY="${ADP_ENV}/platform/terraform.tfstate"

confirm_account() {
  mkdir -p -m 700 "$TASK_DIR" && chmod 700 "$TASK_DIR" || return 1
  caller=$(aws sts get-caller-identity --query Account --output text) || return 1
  aws sts get-caller-identity --query Arn --output text || return 1
  if [ "$caller" != "$ADP_ACCOUNT" ]; then
    echo "WRONG ACCOUNT: resolved $caller, intended $ADP_ACCOUNT" >&2
    return 1
  fi
  # The backend holds the state about to be migrated; confirm this account OWNS it.
  # --expected-bucket-owner is what makes this an ownership check: without it,
  # head-object proves only that the key is readable, which a bucket in some other
  # account could also satisfy. S3 returns 403 when the owner does not match.
  aws s3api head-object --bucket "$STATE_BUCKET" --key "$STATE_KEY" \
    --expected-bucket-owner "$ADP_ACCOUNT" \
    --query 'LastModified' --output text || return 1
  echo "account $caller owns backend s3://$STATE_BUCKET/$STATE_KEY; confirmed"
}
```

**1. Initialise the backend, and confirm it is the one just verified.** This comes
**before** any state read. `terraform state pull` reads whatever backend the
directory was last initialised against, which may be another account or
environment entirely — so initialising afterwards would mean the snapshot and the
plan came from different state. `init` here also resolves the reviewed provider:
discard any plan file saved before this point, since a saved plan is bound to the
provider that produced it (Section 3).

```bash
resolve_backend() {
  # Absolute, so this is idempotent: the chain may re-enter it, and a relative
  # `cd platform/infra` from inside platform/infra fails.
  cd "$REPO_ROOT/platform/infra" || return 1
  terraform init -reconfigure -input=false \
    -backend-config="../../environments/${ADP_ENV}/backend.tfvars" \
    -backend-config="bucket=${STATE_BUCKET}" \
    -backend-config="key=${STATE_KEY}" || return 1
  # Confirm the initialised backend is the bucket/key verified in step 0.
  python3 - <<'EOF' || return 1
import json, os, sys
cfg = json.load(open(".terraform/terraform.tfstate"))["backend"]["config"]
want = (os.environ["STATE_BUCKET"], os.environ["STATE_KEY"])
got = (cfg.get("bucket"), cfg.get("key"))
if got != want:
    sys.exit(f"backend is {got}, expected {want}")
print(f"backend confirmed: s3://{got[0]}/{got[1]}")
EOF
  terraform version   # confirm the AWS provider is >= 6.42.0
}
```

**2. Preserve, and retain the inputs.** Now that the backend is the confirmed one,
snapshot the state and produce the retained configuration, so every later claim of
preservation has a baseline and the plans in steps 3 and 6 use the reviewed values.

```bash
preserve() {
  python3 ../../platform/scripts/upgrade-state.py prepare \
    --directory "$TASK_DIR" --account "$ADP_ACCOUNT" \
    --environment "$ADP_ENV" --region us-east-1 || return 1

  terraform state pull > "$TASK_DIR/pre-migration.tfstate" || return 1
  chmod 600 "$TASK_DIR/pre-migration.tfstate" || return 1
  python3 -c "import json,os;s=json.load(open(os.environ['TASK_DIR']+'/pre-migration.tfstate'));print('serial',s['serial'],'lineage',s.get('lineage'),'managed',sum(len(r.get('instances',[])) for r in s['resources'] if r.get('mode')=='managed'))"
}
```

`prepare` writes `platform.tfvars.json` into `$TASK_DIR`, carrying the retained
values — the EKS public-access CIDRs, the existing cluster-admin principal ARNs,
and the ECR repository encryption settings. Those matter here specifically: EKS
access-entry `principal_arn` is ForceNew and ECR encryption is immutable, so
planning without them proposes destroying a human operator's cluster access or
replacing repositories. `prepare`'s retained snapshot is also an acceptable
explicit baseline for step 4's `--preserved-snapshot` where you prefer the values
it reads over a raw state pull.

**3. Generate the migration plan, read-only.** A full-scope `-refresh-only` plan,
with the committed var file *and* the retained inputs. `-refresh-only` is the
operative flag: it proposes **no resource changes at all**, only state
reconciliation, so it normalises schema across the resources a targeted plan would
leave stale.

```bash
plan_migration() {
  terraform plan -refresh-only -input=false \
    -var-file="../../environments/${ADP_ENV}/platform.tfvars" \
    -var-file="$TASK_DIR/platform.tfvars.json" \
    -out="$TASK_DIR/migrate.tfplan" || return 1
  chmod 600 "$TASK_DIR/migrate.tfplan"
}
```

There is deliberately no `terraform show` here. Step 4 performs the export itself,
so the JSON that gets reviewed is provably this plan's — see below.

**4. Inspect before applying anything — with a check that refuses.** This is the
review the whole exercise exists to protect, so it must be enforced rather than
eyeballed. Run it from this same initialised directory, so it resolves the same
provider that produced the plan:

```bash
inspect_migration() {
  python3 ../../platform/scripts/refresh_only_migration_guard.py \
    --plan-file "$TASK_DIR/migrate.tfplan" \
    --preserved-snapshot "$TASK_DIR/pre-migration.tfstate" \
    --expect-resources 136 || return 1
}
```

The guard takes **only the saved plan**, and runs `terraform show -json` on that
exact file itself, checking the exit code internally. An earlier version took a
caller-supplied JSON plus `--show-exit-code 0`, which meant an operator could hand
it a stale or unrelated export and an asserted success — so the evidence was about
whatever file was passed, not about the plan being approved. The export is held in
memory; pass `--save-json <path>` only if you need a copy, and it is written mode
600.

It **exits non-zero** on any of: a failed export, a body that does not parse, a
document that is not a plan export or that Terraform marks errored/incomplete, an
optional field present with the wrong type, a proposed create/update/delete, a
drift entry proposing `delete`, a state member with no managed resources or a
duplicated address, a managed address present before and absent after, a changed
`id`/`arn`/`name`, a snapshot whose addresses or identities disagree with **either**
plan state member, a snapshot whose *non-empty* `lineage` conflicts with a
*non-empty* one the plan records, or a managed-resource count other than the one
asserted. Pass `--expect-resources` with the count established for the state under
review (136 at the time of writing); omit it rather than guessing.

**An empty `lineage` is absent metadata, not a mismatch.** A real saved plan's
`tfstate-prev` member carries `lineage: ""` and `serial: 0`, while its `tfstate`
carries the state's actual lineage and serial — verified against plans Terraform
wrote. An earlier version of the guard compared the snapshot's real lineage against
that empty string and refused a **genuine** baseline. It now compares only lineages
that are actually recorded, and binds identity against both members; two
*conflicting non-empty* values are still a refusal, because those cannot describe the
same state. Do not work around a lineage refusal by editing a state file or a
snapshot: if two non-empty lineages disagree, you have the wrong baseline.

**An omitted field is not a failure.** Terraform **leaves `resource_changes` out
entirely** when a plan proposes no changes — which is what a correct
`-refresh-only` plan is. The guard treats that as zero entries. (It previously
refused such an export as truncated, i.e. it refused the artifact it exists to
approve; `test_real_refresh_only_export_shape_passes` now pins the correct
behaviour against a fixture of that real shape.) What it refuses instead is a
document that is malformed, truncated, errored, incomplete, or not a plan.

**Why it reads the saved plan file and not just the JSON.** A `-refresh-only`
plan's `planned_values` **can be empty**, precisely because it proposes no
resource changes — so comparing `planned_values` against `prior_state` would
compare nothing to nothing and pass. Preservation is instead read from the two
state documents the saved plan carries internally: `tfstate-prev` (before) and
`tfstate` (the migration's result). That is the artifact pair in which the real
migration was observed to preserve all 136 managed resources with `id`/`arn`/`name`
unchanged, in **both** members. Schema-version differences between them are expected
and permitted — raising them is the point.

**What the guard does not establish:** it reads a saved plan, so it tells you what
that plan would do to state. It makes no AWS call and cannot confirm the resources
exist in the account; the drift leg is Terraform's observation, passed through. A
pass means "safe to apply *this file*", not "safe to apply a freshly generated
one".

**5. Run the review chain — this is the only step above that executes anything.**
Steps 0–4 defined functions; nothing has run yet. Chain them with `&&` in **one**
function so a refusal at any step makes every later step unreachable rather than
merely un-recommended, and run it in a **dedicated shell** — a script file or
`bash <<'EOF'` — never pasted at your own prompt, where `exit` would close your
session.

The apply is deliberately **not** in this chain, and neither is the scoped plan.
Everything here is read-only; step 6 is the state write, and it is root's decision
after reading step 4's output, not something a passing guard should trigger.

```bash
review_migration() {
  confirm_account \
    && resolve_backend \
    && preserve \
    && plan_migration \
    && inspect_migration
}

if review_migration; then
  echo "REVIEWED: $TASK_DIR/migrate.tfplan is safe to apply. Apply THAT file only."
else
  echo "REFUSED at the first failing step above. Do not apply. Do not edit state." >&2
  exit 1
fi
```

Verify the chain stops before you trust it — with a deliberately failing step, no
later step may run:

```console
bash <<'EOF'
set -euo pipefail
confirm_account() { echo "wrong account"; return 1; }
resolve_backend() { echo "REACHED-RESOLVE"; }     # must NOT appear
review_migration() { confirm_account && resolve_backend; }
review_migration || { echo "stopped correctly"; exit 1; }
echo "REACHED-APPLY"                              # must NOT appear
EOF
# expected: "wrong account", "stopped correctly", exit 1 — and neither REACHED- line.
```

**6. Apply the saved refresh-only plan — root only, and outside the script above.**
Apply *that exact saved file*, never a freshly generated one, so what was reviewed is
what is applied. This writes **state only**; it makes no cloud change. Run it as a
separate deliberate command after reading step 4's output.

```bash
cd "$REPO_ROOT/platform/infra"
terraform apply "$TASK_DIR/migrate.tfplan"
```

**7. Now the targeted rollout plan.** Generate a fresh targeted plan with the same
retained inputs and export it. With the schema normalised, the export succeeds and
the plan becomes reviewable. Use the same var files as step 3 — a scoped plan built
from defaults is not the deployment that was reviewed.

```bash
plan_scoped() {
  terraform plan -input=false \
    -var-file="../../environments/${ADP_ENV}/platform.tfvars" \
    -var-file="$TASK_DIR/platform.tfvars.json" \
    -target=module.eks.aws_eks_cluster.main \
    -out="$TASK_DIR/scoped.tfplan" || return 1
  chmod 600 "$TASK_DIR/scoped.tfplan" || return 1
  terraform show -json "$TASK_DIR/scoped.tfplan" > "$TASK_DIR/scoped.json" || return 1
  chmod 600 "$TASK_DIR/scoped.json"
}
plan_scoped || exit 1   # export failed: the schema is not normalised
```

Unlike step 4, the `terraform show` here is written out because the export is what
you review. Keep the `|| return 1`: a failed export at this point means the migration
did not achieve what it was for, and a truncated `scoped.json` must not be read as
though it were complete.

**Do not expect `verify_scoped_plan.py` to gate this plan.** An earlier revision of
this runbook said to enforce the narrow scope with it; that was wrong. Its `SCOPES`
table carries only the two `network-policy-controller` entries for
`module.eks.kubernetes_config_map.amazon_vpc_cni[0]`, and it **refuses** any scope
name without an entry rather than defaulting to permissive — so there is nothing in it
for the capacity-subnet change to the EKS cluster, as merged #5830 documents. Pointing
an operator at a guard that cannot cover their plan is worse than pointing at none: it
invites either a bypass or a false sense of having been gated.

So for this target, **root inspects that exact saved plan directly** and confirms it
carries the single in-place cluster change and nothing else. No new guard scope is
requested here; extending `SCOPES` is out of scope for #5831.

**8. Clean up.** The task directory holds state and plans.

```bash
shred -u "$TASK_DIR"/*.tfplan "$TASK_DIR"/*.tfstate "$TASK_DIR"/*.json 2>/dev/null || rm -f "$TASK_DIR"/*
```

**What was verified where — read this before citing any of it as evidence.**

| Step | Who ran it | What was established |
|---|---|---|
| 3 — `-refresh-only` generation and export | **root only**, against the real backend | The plan generates and its JSON exports; all 136 managed resources and IDs preserved; no drift deletions; no resource changes. **The worker never ran this step in any form** |
| 4 — the guard's logic, offline | worker | Refuses resource loss, identity change, a snapshot identity/lineage mismatch, an ordinary full plan, a refreshed deletion, a failed or unparseable export, a document that is not a plan or is errored/incomplete, a malformed optional field, an empty or duplicated managed set, a plan missing its state members, and a count mismatch; passes both permitted export shapes |
| 4 — the guard against plans Terraform really wrote | worker, with the real `terraform` binary | Refuses an unexportable targeted plan on the real non-zero `show` exit, and refuses an ordinary full plan after reading the real ZIP's state members. Both plans were **ordinary plans with `-refresh=false`** over the synthetic fixture — not `-refresh-only`, and not against any real backend |
| 5 — the apply | **nobody** | Not executed. No state write has occurred |

**What the worker did not run.** The worker never executed step 3. A `-refresh-only`
plan cannot be produced offline: it exists to query the provider, so without
credentials it fails with `AuthFailure` rather than planning — verified, including
with credential stubs and the metadata endpoint disabled. Every plan the worker
generated was an **ordinary plan with `-refresh=false`** against the synthetic
fixture in `.github/scripts/tests/fixtures/platform-mixed-state-5831/`.

**What root established, and its boundary.** Root generated the real
`-refresh-only` plan against the real backend and exported it read-only, observing
all 136 managed resources with `id`/`arn`/`name` unchanged, no drift deletions and
no resource changes. Root has **not applied it**. So the permitted case — a real
refresh-only plan passing this guard — rests on root's observation plus root
re-running the corrected guard against that plan; no worker test demonstrates it,
and none can.

### 5c — Why not an ordinary full apply

A full-scope plan **does** export, which can make `terraform apply` look like a
quicker way to normalise state. **It is not, and the difference is destructive.**

`-refresh-only` reconciles state and proposes no resource changes. An ordinary
full apply acts on the difference between source and state — and that difference
currently includes an add-on present in state that the source does not declare,
so the plan proposes **destroying it**. On the real cluster that resource is
CoreDNS, i.e. cluster DNS.

`test_full_scope_plan_would_destroy_the_undeclared_add_on` exists to keep this
advice honest: if this document ever drifts toward "just run a full apply", that
assertion is what should stop it.

### 5d — The undeclared CoreDNS add-on

**What is observed:** the state records an `aws_eks_addon` for CoreDNS that the
committed platform source does not declare. `platform/infra` declares only
`amazon-cloudwatch-observability` and `metrics-server`. The only
`aws_eks_addon "coredns"` definition in the repository is on the unmerged branch
`fix/management-coredns` (`platform/infra/modules/eks/dns.tf`).

**What is not established: what manages that add-on now.** An earlier revision of
this runbook asserted that EKS Auto Mode manages CoreDNS, citing
`bootstrap_self_managed_addons = false`. That inference does not hold — that
setting governs whether the cluster bootstrapped self-managed add-ons **at
creation time** and says nothing about current ownership. Ownership is unresolved
and needs its own investigation.

**The safe consequence holds either way,** which is why the sequence above does
not depend on the answer: because state records a resource the source does not
declare, any plan whose scope includes it proposes destroying it. So preserve the
add-on, keep it out of scope, and confirm the resource-action scope of every plan
before applying. `.github/scripts/verify_scoped_plan.py` enforces that narrowness
by refusing any plan with collateral changes. Resolve the declaration question
separately from this provider work.

### 5e — Standing constraints

**Discard any plan saved before the upgrade.** A saved plan is bound to the
provider that produced it, so a pre-upgrade plan file cannot be exported or
applied afterwards; it fails with a *different*, easily-misread error (Section 3).

Platform apply is **manual-only** (`platform-infra-apply.yml` is
`workflow_dispatch`, no `push`). A provider-constraint change therefore cannot
cause an unreviewed apply. `test_platform_apply_remains_manual_only` asserts that
property so it cannot be relaxed silently.

## Section 6 — The regression guards

Three suites: the two defects in Section 1 and Section 5a, plus the step-4
preservation guard.

### 6a — `test_platform_mixed_state_export.py`

Reproduces the mixed-age state behaviour against a committed fixture
(`.github/scripts/tests/fixtures/platform-mixed-state-5831/`): the targeted plan
fails to export, the same plan exports once the stale record is at the provider's
schema version, a full-scope plan exports, and that full-scope plan proposes
deleting the undeclared add-on. It also asserts this runbook still documents the
refresh-only sequence — a mechanism nobody can follow is not a fix.

**The fixture carries records of two different ages on purpose.** The first
attempt at #5831 used a fixture with only the newer add-on record; it passed on
the raised floor while the real targeted plan still failed. A fixture younger
than the real state certifies the wrong thing. See that directory's README.

Every leg runs in a temp copy with `-refresh=false` and every `AWS_*` variable
stripped, against synthetic state — no AWS call, no real state, no apply. The
`-refresh-only` **apply** is deliberately not executed: it writes state, so it is
root-operated, and what this suite asserts about it is that the runbook describes
it, not that a test performed it.

```bash
cd .github/scripts && pytest tests/test_platform_mixed_state_export.py -v
```

The Terraform-dependent legs skip when no `terraform` binary is present **and CI is
not set**; the fixture-integrity and runbook assertions still run. Under `CI`, a
missing binary *or* a failed `terraform init` is a **failure**, not a skip —
otherwise dropping the workflow's `setup-terraform` step, or a provider that stops
resolving, would report green having exercised only the fixture's shape. Both
branches were verified by injection: pointing the fixture at a nonexistent
provider source makes `init` fail, and the suite then errors with `CI` set and
skips with it unset.

### 6c — `test_refresh_only_migration_guard.py`

Covers `platform/scripts/refresh_only_migration_guard.py`, the step-4 check. The
suite's purpose is the **refusals**: each prohibited condition asserts a non-zero
exit, because the snippet this guard replaced printed counts and exited 0 whatever
they were, while the text around it claimed it refused an unsafe plan. A check that
cannot fail is worse than no check — it reads as a gate, so it is trusted.

Fixtures are synthetic saved-plan ZIPs built in the test, for the reason given in
Section 5b: a real `-refresh-only` plan cannot be generated offline. The Terraform
invocation is **stubbed** — a fake binary that records its argv and emits a chosen
document and exit code — which is what lets the malformed, errored and
failed-export legs be driven at all, and lets one test assert the guard passed the
exact `--plan-file` rather than some other path. So this suite establishes the
guard's logic, not the safety of any particular real plan. It needs no `terraform`
binary, makes no AWS call, and writes no state.

Two assertions carry more weight than the rest:

* `test_real_refresh_only_export_shape_passes` reads
  `fixtures/platform-mixed-state-5831/real-refresh-only-export.json`, the sanitized
  top-level key set of root's real successful export — `resource_changes` absent —
  and requires a **pass**. This is the regression for the guard having refused that
  real artifact. Do not add `resource_changes` to that fixture to satisfy a test.
* `test_guard_links_export_to_the_exact_plan_file` asserts the guard exported the
  plan it was given, which is what replaced the old caller-asserted
  `--show-exit-code`.

Because the Terraform call is stubbed here, the complementary legs live in
`test_platform_mixed_state_export.py`, which has a real binary:
`test_migration_guard_refuses_a_real_plan_whose_export_fails` and
`test_migration_guard_reads_a_real_saved_plan_and_refuses_its_actions` run the guard
against saved plans Terraform actually wrote. Those establish the guard works on
real Terraform output; they still do not establish that any real plan is safe.

```bash
cd .github/scripts && pytest tests/test_refresh_only_migration_guard.py -v
```

### 6b — `test_platform_provider_constraint.py`

Asserts the floor holds, the root and child constraints stay identical, the major
stays bounded, and platform apply stays manual-only.

It reads the Terraform files **as text**, which is weaker than a plan assertion
and is used deliberately: `required_providers` is resolved by `terraform init`,
before any test runs, and is not part of the plan graph, so **no
`terraform test` assertion can see it**. Every `.tftest.hcl` under
`platform/infra` could pass while the root module pinned a provider that cannot
decode live state.

It is registered in `script-tests.yml`, which already runs on `platform/infra`
paths on both `push` and `pull_request`, and is pinned by filename rather than
globbed per that workflow's stated convention. The guard was confirmed to
**fail** on the original `~> 5.0` constraint, not merely to pass on the
corrected one.

```bash
cd .github/scripts && pytest tests/test_platform_provider_constraint.py -v
```

If a future provider upgrade is needed, raise `MINIMUM_AWS_MAJOR_MINOR` and
`EXPECTED_CONSTRAINT` together, re-run the Section 3 reproduction against a
state record from the target provider, and re-run all three module suites.
