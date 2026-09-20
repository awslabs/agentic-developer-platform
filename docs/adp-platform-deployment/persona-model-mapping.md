# Persona model mapping in new deployments

Settings → Agent Models lets a user save a model for each compatible persona.
The initiating human's mapping is resolved at dispatch for direct, delegated,
chat and AI-DLC invocations. Claude Sonnet 5 is included as
`global.anthropic.claude-sonnet-5`, with CLI alias `sonnet5`.

## Deployment defaults

Basic mapping defaults to enabled in the gateway, webhook and agent-factory
Terraform modules. The factory enables its chat producer only when a gateway
is deployed. Standard full deployment installs the preference schema, producer
identity and lookup wiring, gateway, webhook code and chat/runtime images.
No worker-security cutover or recurring model probe is required for basic mapping.
Protected authority and chat model-policy activation remain separate opt-ins.

Gateway Terraform publishes the mapping setting to
`/adp/<environment>/gateway/persona-model-mapping-enabled`. Both the GitHub
workflow and `platform/scripts/deploy-all.sh` read it to configure the gateway
and default Agent Models UI visibility. An existing
`/adp/<environment>/gateway/feature-agent-models` parameter explicitly overrides
UI visibility. To disable mapping, set `persona_model_mapping_enabled=false`
consistently in the gateway, webhook and factory module inputs; a UI override
only controls visibility.

The model catalogue, aliases, producer satellite and SDK request-shape manifests
are versioned in the repository and shipped with each release. Catalogue and
manifest parity tests catch incomplete additions. The CLI and UI fetch the
server catalogue, so adding a model does not require a local CLI reinstall.
The catalogue is curated; it does not automatically admit every new Bedrock model.

Full deployment already runs `platform/scripts/enable-bedrock-models.sh`, which
discovers ACTIVE Anthropic models and enables their Marketplace agreements.
Account/region availability and organization Marketplace restrictions still
apply. Local SDK request capture verifies request construction, not successful
provider inference in a new account.

## Verification after deployment

After login, confirm the catalogue and validate a choice without changing the
user's saved mapping or invoking a model:

```bash
adp models catalog --persona developer --json
adp models mappings set --persona developer --model sonnet5 --dry-run --json
```

Sonnet 5 should appear as selectable, and Settings → Agent Models should be
visible. For each new AWS account/region, check Bedrock model availability and
complete one bounded persona invocation using a test user's saved choice before
claiming live acceptance. Automated deployment-renderer tests cover a new
`integration` environment with no dev overlay and preserve explicit overrides;
these tests do not provision or certify a fresh AWS environment.
