# Kimi K3 through the ADP gateway

Kimi Code can use its `openai_responses` provider against
`<gateway>/api/openai/v1`. The gateway forwards Responses requests to Amazon
Bedrock Runtime, signs with the resolved AWS destination credentials, preserves
streaming/tool/reasoning payloads, and records usage through the pricing ledger.

Supported IDs are `global.moonshotai.kimi-k3` and
`us.moonshotai.kimi-k3`. Use the explicit profile ID in Responses requests.
The model-list aliases `kimi-k3` and `kimi-k3-us` identify those profiles.
Explicit tenant allowlists still need to grant access; a gateway route permission
does not override a tenant denial. Bedrock access must be verified in the user's
resolved destination account, which may differ from the platform hosting account.

This release supports Kimi through Responses. The legacy Chat Completions
translator constructs Claude payloads and is not a Kimi transport. Hosted Kimi
personas and a bundled `adp kimi` launcher are outside this gateway release.

## Pricing and rollout

Snapshot `2026-09-24.2` adds 111 reviewed K3 endpoint/geography/tier variants.
Migration `073_kimi_k3_pricing` preserves earlier rates, generations and operator
flags. Deploy the gateway and both budget Lambda archives together and use the
canonical pricing finalizer. Existing source gaps may still require the reviewed
`--allow-partial-refresh` recovery procedure.

Source: [AWS Kimi K3 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html).
Standard USD per million tokens:

| Profile | Input | Output | Cache read | Cache write, 30 minutes |
|---|---:|---:|---:|---:|
| Global | 3.00 | 15.00 | 0.30 | 3.75 |
| US | 3.30 | 16.50 | 0.33 | 4.125 |

The card publishes Priority at 1.75 times Standard and Flex at 0.5 times Standard.
The daily parser rejects changes to units, table shape or multipliers for review.
The 1M context ceiling is recorded separately from flat pricing thresholds.
Policy-governed Responses calls require an explicit output cap and an evidenced
budget bound; Kimi does not bypass the existing quote/refusal checks.

## Isolated CLI validation

Use the CLI regression EC2 profile/subnet and a disposable OS user. Install the
current Kimi Code CLI (`MoonshotAI/kimi-code`), not the archived Python CLI.
Never install or modify CLI configuration on the operator's EC2 machine.

Configure an isolated Kimi home with the following provider, after starting the
ADP local token-refresh proxy for the dedicated regression login. Supply its
local capability in `ADP_KIMI_LOCAL_CAPABILITY`; never put AWS credentials in Kimi.

```toml
default_model = "adp-kimi-k3"

[providers.adp]
type = "openai_responses"
base_url = "http://127.0.0.1:9191/openai/v1"
api_key_env = "ADP_KIMI_LOCAL_CAPABILITY"

[models.adp-kimi-k3]
provider = "adp"
model = "global.moonshotai.kimi-k3"
max_context_size = 1000000
max_output_size = 1024
capabilities = ["thinking", "image_in", "tool_use"]
```

Validate a bounded read-only file task, confirm tool-result continuation, and
inspect gateway usage for the exact model, destination, successful requests and
priced tokens. Verify streaming and upstream errors separately. Remove temporary
sessions, test resources and the EC2 instance afterward. A successful catalogue
lookup or initialized CLI session alone is not successful inference.
