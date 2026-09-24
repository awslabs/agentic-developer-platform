# Runbook: The Platform AWS Provider Floor

Covers why `platform/infra` requires AWS provider >= 6.42.0, how the failure it
fixes presents, how to reproduce and verify it without touching a live account,
and what to check before the next scoped apply.

**Issue:** #5831 · **Parent epic:** #3959 · **Deployment prerequisite for:** #5830

---

## Section 1 — What was wrong, and why it was invisible

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

## Section 5 — Before the next scoped apply

**The CoreDNS add-on in state is not declared in the platform source.** The
cluster runs EKS Auto Mode (`bootstrap_self_managed_addons = false`), which
manages CoreDNS itself; `platform/infra` declares only the
`amazon-cloudwatch-observability` and `metrics-server` add-ons. The only
`aws_eks_addon "coredns"` definition in the repository lives on the unmerged
branch `fix/management-coredns` (`platform/infra/modules/eks/dns.tf`).

The consequence matters for apply safety: because the record contains a
resource the committed configuration does not declare, **a full untargeted plan
proposes destroying it**. This is exactly what the narrow resource-action scope
enforced by `.github/scripts/verify_scoped_plan.py` exists to catch. Confirm the
resource-action scope of any fresh plan before applying, and resolve the CoreDNS
declaration question separately from the provider floor.

**Discard any plan saved before the upgrade.** A saved plan is bound to the
provider that produced it, so a pre-upgrade plan file cannot be exported or
applied after the floor is raised (see Section 3 for the misleading error it
produces). Generate a fresh plan after `init` resolves the new provider.

Platform apply is **manual-only** (`platform-infra-apply.yml` is
`workflow_dispatch`, no `push`). A provider-constraint change therefore cannot
cause an unreviewed apply. The guard described below asserts that property so it
cannot be relaxed silently.

## Section 6 — The regression guard

`.github/scripts/tests/test_platform_provider_constraint.py` asserts the floor
holds, the root and child constraints stay identical, the major stays bounded,
and platform apply stays manual-only.

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
