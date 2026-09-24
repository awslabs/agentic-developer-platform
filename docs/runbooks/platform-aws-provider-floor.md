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
Read from `terraform providers schema -json`, not from release notes, and
asserted by `test_no_single_provider_version_satisfies_both_records`.

A local state write (`terraform state mv`, for instance) does **not** upgrade the
schema — verified. The upgrade is persisted only by an apply whose scope includes
the resource. Hence the sequence below.

### 5b — The sequence

Each step is read-only until step 5, which is **root-operated** and writes state
only. Do not compress these steps; the ordering is what keeps the migration
reviewable.

**1. Preserve.** Snapshot the state and record the upgrade inputs before
anything else, so every later claim of preservation has a baseline to compare
against.

```bash
terraform state pull > /tmp/pre-migration.tfstate
python3 -c "import json;s=json.load(open('/tmp/pre-migration.tfstate'));print('serial',s['serial'],'resources',len(s['resources']))"
```

**2. Resolve the reviewed provider.** `init` so the constraint resolves to a
6.42.0+ provider. Discard any plan file saved before this point — a saved plan is
bound to the provider that produced it (Section 3).

**3. Generate the migration plan, read-only.** A full-scope `-refresh-only` plan.
`-refresh-only` is the operative flag: it proposes **no resource changes at all**,
only state reconciliation, so it normalises schema across the resources a
targeted plan would leave stale.

```bash
terraform plan -refresh-only -out=/tmp/migrate.tfplan
terraform show -json /tmp/migrate.tfplan > /tmp/migrate.json
```

**4. Inspect before applying anything.** This is the review the whole exercise
exists to protect. Confirm all three properties hold:

```bash
python3 - <<'PY'
import json
plan = json.load(open('/tmp/migrate.json'))
pre = json.load(open('/tmp/pre-migration.tfstate'))
changes = [c for c in plan.get('resource_changes', [])
           if c['change']['actions'] != ['no-op']]
drift = plan.get('resource_drift', [])
print('resource changes (must be 0):', len(changes))
print('drift entries proposing delete (must be 0):',
      sum(1 for d in drift if 'delete' in d['change']['actions']))
print('managed resources in pre-migration state:', len(pre['resources']))
PY
```

- **No resource changes.** A `-refresh-only` plan that proposes creating,
  updating or destroying anything is not a state migration; stop and re-review.
- **No drift deletions.** A drift entry proposing `delete` means Terraform did
  not find the resource in the account. Applying that removes it from state.
- **Every managed address and ID still present.** Compare the addresses in the
  plan against the snapshot from step 1. Root verified all **136** managed
  addresses and IDs preserved on the real state.

**5. Apply the saved refresh-only plan — root only.** Apply *that exact saved
file*, never a freshly generated one, so what was reviewed is what is applied.
This writes **state only**; it makes no cloud change.

```bash
terraform apply /tmp/migrate.tfplan
```

**6. Now the targeted rollout plan.** Generate a fresh targeted plan and export
it. With the schema normalised, the export succeeds and the scoped-plan guard can
read it.

**What was verified where.** Steps 1–4's commands were executed against the
synthetic fixture, credential-free, and the step-4 script was confirmed
*discriminating*: run against a full-scope plan it reports a non-zero change
count, i.e. it refuses the plan an operator must not apply. The `-refresh-only`
plan and export against the **real** backend were verified by root, who observed
all 136 managed addresses and IDs preserved, no drift deletions and no resource
changes. Step 5 has **not** been executed by anyone at the time of writing: no
state write has occurred.

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

Two suites, covering the two defects in Section 1 and Section 5a respectively.

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

The Terraform-dependent legs skip cleanly when no `terraform` binary is present;
the fixture-integrity and runbook assertions still run.

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
