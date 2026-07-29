# GPT-5.6 Family Smoke Test

**Date:** 2026-07-28

Gateway passthrough verification for the GPT-5.6 model family (Sol/Terra/Luna, GA 2026-07-13) via `adp-gateway` provider (sigv4-proxy → API GW → gateway pod → bedrock-mantle, `openai/v1/responses` path).

## Results

| Model | Reply (verbatim) | Session ID | Tokens Used | Exit Code |
|-------|-------------------|------------|-------------|-----------|
| `openai.gpt-5.6-sol` | SOL-VIA-GATEWAY-OK | `019fa845-87a9-7e80-ab99-caeb6eaa18e2` | 8,903 | 0 |
| `openai.gpt-5.6-terra` | TERRA-VIA-GATEWAY-OK | `019fa845-a27d-7ce2-bbf9-fba6acb3a4b8` | 8,905 | 0 |
| `openai.gpt-5.6-luna` | LUNA-VIA-GATEWAY-OK | `019fa845-bd37-7412-a458-2f742eec0bb3` | 8,905 | 0 |

## Configuration

- **Codex CLI**: v0.145.0
- **Provider**: `adp-gateway` (ADP Gateway bedrock-mantle passthrough)
- **Wire API**: `responses` (`/openai/v1/responses`)
- **Auth**: SigV4 via pod IRSA → sigv4-proxy sidecar (127.0.0.1:9090)
- **Tenant**: `aws-e` (dev)

## Notes

- All three variants returned exact expected reply strings, confirming real inference (not fabrication).
- The gateway `mantle_allowed_models` default (`openai.*`) covers the GPT-5.6 IDs without configuration changes.
- Model metadata warnings ("not found, defaulting to fallback") are cosmetic — Codex CLI lacks built-in metadata for newly-GA models; inference is unaffected.
- Bedrock IAM (`bedrock-mantle:CreateInference`) on the project already covers these model IDs via the existing `openai.*` wildcard grant.
