# Prepare the protected Atlassian provider plan

The deployment operator can generate the provider plan directly from Atlassian's
public metadata. Users do not need to copy metadata, create an OAuth application,
or provide a personal access token. Run `scripts/prepare-atlassian-mcp.py` with the
approved deployment identity after Terraform creates the separate plan and
client-state secret containers. The script requires Python and boto3; it does not
add an AWS CLI dependency to the ADP client.

The release context supplies these arguments:

```bash
python3 scripts/prepare-atlassian-mcp.py \
  --account-id "$ADP_TARGET_ACCOUNT" \
  --region "$ADP_REGION" \
  --environment "$ADP_ENVIRONMENT" \
  --gateway-url "$ADP_PUBLIC_URL" \
  --contract-revision "$ADP_REVIEWED_SOURCE" \
  --output-dir "$ADP_PRIVATE_PLAN_DIR"
```

The default invocation checks the active AWS account, discovers the two existing
Terraform-owned secret containers, retrieves the exact v2 resource and issuer
metadata, and validates the required scopes, endpoints, PKCE and refresh support.
It creates a private directory containing `provider-plan.json` and
`atlassian-mcp.tfvars.json`. Neither file should be committed or published in a
public workflow artifact. Standard output contains the metadata pins and result,
without provider credentials or the gateway URL.

Within the authorized, serialized release workflow, repeat the same command with
`--publish` to fill the empty plan secret and independently read it back. An
identical retained plan is a no-op, including after a lost publication response.
A different existing plan is a conflict requiring reviewed rotation; the script
does not replace it. Repeated runs use the retained contract revision, rather
than changing it for every unrelated deployment. The client-registration secret
is never read or changed by this command.

Pass the generated Terraform variable file to the reviewed gateway infrastructure
plan/apply so Terraform publishes the corresponding revision and metadata digest
to SSM. The existing gateway and recovery deployment renderers then consume those
SSM bindings. Publishing the secret alone does not activate those bindings;
later applies rebuild the private variable file from the retained plan.
The maintained **Gateway Infra Apply** workflow performs this wiring without
operator-assembled pins: first run the normal reviewed manual apply to create
the Terraform-owned containers. On a later reviewed manual apply, set
`publish_atlassian_mcp_plan=true`. The same trusted deployment identity reads
the advertised metadata, fills only an empty plan, and includes its private pins
in the Terraform plan. Existing retained plans are reused by subsequent applies
and the manual **Gateway Infra Plan**, including after unrelated source changes;
the post-apply convergence check uses the same pins. By default the workflows
derive the HTTPS gateway origin from the target's initialized Terraform state
(`frontend_cloudfront_domain_name`), so first publication needs no new URL
variable. Custom-domain deployments can instead set the protected
`ADP_FOUNDATION_GATEWAY_URL` override in the deployment environment. Keep that
override for later applies: the retained plan origin is checked on reuse, and
a changed origin or retained plan fails closed pending reviewed rotation. The
ARC runner includes Python and boto3. Neither workflow
dispatches another workflow, and an initial run without containers does not
silently publish a plan. Do not upload the runner's private pin files as artifacts.
Use the staged IAM update in [deployment configuration](deployment-configuration.md)
before the final gateway rollout. This helper does not dispatch infrastructure
workflows or bypass their deployment identity and review requirements.

This step establishes a metadata configuration, not live Identity compatibility,
an organization policy observation or a user grant. The leased setup worker owns
dynamic client registration. The real Identity probe, provider policy observation
and user OAuth consent still determine readiness and acceptance.
