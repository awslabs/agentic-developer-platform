# Bedrock readiness during deployment

`./deploy.sh` prepares Anthropic model access through
`platform/scripts/enable-bedrock-models.sh`. The same helper runs from
`deploy-all.sh` and the platform infrastructure workflow. There is no list of
all models to maintain: optional models are discovered from Bedrock, and
required defaults are read from the Python worker entrypoint and the chat
ConfigMap (including its summarization model). Today these are global Opus 5
and global Sonnet 4.6. Changing either execution source changes the checks.

## First-use registration

If the account already has authorization, including authorization inherited
from its AWS Organization, no registration is submitted. Otherwise, the helper
checks for an existing Anthropic first-use registration. It never replaces an
existing registration. For an unregistered account, supply the organization's
actual form details as a JSON file:

```bash
AWS_PROFILE=customer AWS_REGION=us-east-1 \
  ./deploy.sh --anthropic-use-case /secure/path/anthropic-use-case.json
```

For direct `deploy-all.sh` or helper use, set
`ADP_BEDROCK_USE_CASE_FILE=/secure/path/anthropic-use-case.json`. This environment
variable also works in automation; the JSON must be available to the deployment
process. Do not commit organization registration data to the repository.

The JSON contains `companyName`, `companyWebsite`, `intendedUsers`,
`industryOption`, `otherIndustryOption` and `useCases`, as documented by
[AWS PutUseCaseForModelAccess](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_PutUseCaseForModelAccess.html).
Use the organization's real first-use form values. ADP does not invent them.
A registration from the AWS Organizations management account can cover member
accounts. Missing registration input stops the model-access step with the
command needed to supply it; it does not pretend Marketplace acceptance alone
has established access.

The deployment identity needs `bedrock:GetUseCaseForModelAccess` and
`bedrock:PutUseCaseForModelAccess` when registration is needed, plus the existing
Bedrock availability/profile/list/agreement permissions and Marketplace
subscription permissions. An organization Private Marketplace restriction
still needs to allow the requested model; this script does not change that policy.

## Readiness and verification

Required models must report an available agreement, `AUTHORIZED`, available
entitlement and available region. The exact inference profiles used by the
runtimes must also be active in the deployment region. The helper waits up to
180 seconds for activation, then fails clearly. Missing fields, denied API calls
or unknown defaults are failures. Optional models cannot hide a required-model
failure. Explicit helper model arguments or `BEDROCK_MODELS` add required models;
they never remove the runtime defaults.

Fresh deployments and updates perform the access checks; teardown and
`deploy-all.sh --ci` do not prepare access or run paid model verification.
Before reporting a
successful deployment, `deploy.sh` runs one direct Bedrock invocation for each
distinct default, capped at eight output tokens per request. `deploy-all.sh`
does the same when run directly; the wrapper defers that check until after its
agent phase, avoiding duplicate verification. There are no automatic inference
retries, tool loops or recurring probes. These small calls incur normal model
usage charges and use the deployment identity. They establish model invocation,
not end-to-end worker, gateway, GitHub or AI-DLC acceptance.

Standalone commands:

```bash
# No AWS calls or changes; display the derived defaults.
bash platform/scripts/enable-bedrock-models.sh --dry-run

# Metadata checks only: no agreement changes, registration or paid inference.
bash platform/scripts/enable-bedrock-models.sh --check

# Metadata checks plus one bounded invocation per default; no access mutations.
bash platform/scripts/enable-bedrock-models.sh --verify

# Prepare account access, using a form only if registration is needed.
bash platform/scripts/enable-bedrock-models.sh --use-case-file /secure/path/form.json
```

Readiness checks run against the active AWS profile and selected region. A new
account/region is only verified after its own checks and invocations pass.
