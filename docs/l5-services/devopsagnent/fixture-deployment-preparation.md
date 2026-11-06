# Prepare the two dedicated DevOps fixtures

`modules/L5-services/scripts/prepare-devops-fixtures.py` builds separate private
deployment bundles for the synthetic queue worker and archive receipt sample.
Preparation reads only the selected local Git commits. It performs no AWS call,
deployment, Product write, native submission, credential forwarding or browser
operation. Existing output directories and state are never overwritten.

The input JSON has exactly these fields:

```json
{
  "schema_version": 1,
  "account_id": "000000000000",
  "region": "us-east-1",
  "queue": {
    "repository": "/private/queue-worker",
    "commit": "0000000000000000000000000000000000000001",
    "fixture_id": "0000000000000001"
  },
  "archive": {
    "repository": "/private/archive-receipts",
    "commit": "0000000000000000000000000000000000000002",
    "fixture_id": "0000000000000002"
  }
}
```

Use real selected values in a private input file. Each fixture identity is a
distinct stable 16-character hexadecimal value. The queue repository must have
the selected bounded `index.mjs`; the archive repository must have its reviewed
`main.tf` and a `bucket_name` variable. The archive Terraform source is copied
byte-for-byte, so its resource definition must be reviewed independently.

```sh
python3 modules/L5-services/scripts/prepare-devops-fixtures.py \
  --config /private/fixture-input.json --output /private/new-fixture-bundle
```

The bundle contains deterministic queue ZIP bytes, a digest manifest, provider
account restrictions and separate local Terraform state paths. Preserve the
private bundle and state through all stages. This root is outside the platform
installation and is never selected by the L5 service release workflow.

## Queue deployment stages

The queue root creates only its own VPC, subnet, zero-rule security group,
encrypted queue, execution role, function and selected event-source mapping.
There is no gateway, NAT, network endpoint, peering or route to another network.
There is no Lambda URL, invocation resource policy or trust in another role.
The generated ZIP must still match its recorded hash when Terraform plans.

1. **Bootstrap:** Lambda's service provisions its ENI in the fixture subnet and
   group. Function-originated network API calls are explicitly denied. No
   published version or queue mapping is created. This phase is not a qualified
   fixture and must not be left as the completed installation.
2. **Sealed:** Remove temporary ENI permissions, retain only the exact queue
   read policy with explicit action/resource denies, publish the immutable
   function version and create a disabled mapping to that version.
3. **Active:** Explicitly enable that mapping after sealed policy and function
   readback. It keeps concurrency two, batch size five and partial batch failure
   handling. This does not enable any L5 service consumer or submit native work.

In the queue bundle, confirm the operator's selected account, then initialize
and save the first plan:

```sh
aws sts get-caller-identity
terraform init -backend-config=backend.json
terraform plan -var-file=bootstrap.tfvars.json -out=bootstrap.tfplan
terraform show bootstrap.tfplan
```

Apply the reviewed saved plan only through the approved operator deployment
path. After readback, plan and review `sealed.tfvars.json` against that retained
state, then separately plan `active.tfvars.json`. Do not precompute later plans
against empty state or skip the sealed policy readback. A later phase never
requires re-enabling bootstrap just to repeat verification. Replacing a function
or rebuilding its network requires a new reviewed deployment procedure.

For the archive bundle, initialize and plan with `fixture.tfvars.json`. The
copied source defines the actual resource changes. The existing sample creates
a private, encrypted, versioned receipt bucket with retention policy; its Python
receipt calculation does not write to the bucket. Deploying the bucket does not
create an executable archive verification target.

## Product and verification installation still required

The [queue producer](queue-verification-producer.md) can validate the sealed
deployment after activation, but infrastructure outputs do not grant Product
authority. Complete these steps through the supported owner APIs:

1. Select each repository in the existing GitHub installation. Read the
   Product's source options and bind the returned repository commit reference;
   a locally known Git SHA cannot replace an installation-authorized reference.
2. Read the current service configuration and binding. Configure the intended
   source and capability through the Product configuration API with its current
   revision. Read `services/devops/delegation-scope`, then use its exact scope
   digest and configuration ETag for a bounded `services/devops/delegations`
   request. Retain the returned grant revision; do not construct SQL grants.
3. Select private versioned evidence storage and an asymmetric signing key.
   Producer observation needs only the exact fixture read permissions listed in
   the producer guide; invocation is restricted to the numeric version. S3
   deletion is restricted to owned payload versions, and KMS is restricted to
   the selected key. The deployment role is not the producer execution role.
4. Bind the producer to owner-authorized provider sessions and request-specific
   signed admission evidence. The existing process-wide evidence reference is
   not a concurrent per-Product registry. Do not install broad Gateway fixture
   permissions or reuse a fixture across unrelated owners.
5. Qualify an actual native task's immutable asset association and individual
   scenario result schema. Then mount the independent producer and native result
   reader in the installed DevOps release-verification adapter. The current
   runtime deliberately mounts only investigation and release readiness.

Steps 4 and 5 are remaining runtime implementation and native qualification.
This preparation package does not claim that those steps are implemented or
that a completed native task alone proves scenario acceptance. The archive
sample additionally needs a separately reviewed executable target and oracle.
