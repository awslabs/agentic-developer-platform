These are AWS publications retrieved on 2026-09-12, sanitized by keeping the
complete `## Pricing` section of each card and only the 64 OpenAI catalog SKUs
and their unmodified OnDemand terms. No modelId fields or joined SKUs are invented.

- Cards: `https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-{filename}.md`
- Catalog: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/us-east-1/index.json`
  (publication 2026-09-11T12:44:08Z, version 20260911124408).
- `astra-bold-scope.md` preserves the earlier same-day Markdown format: bold
  scope/context paragraphs instead of headings. Both forms are published by AWS.

The current cards revise GPT-5.4/5.5 to short/long commercial contexts and
Daybreak to the Sol rates. These are adopted as source changes, not rejected
merely because the source hash changed. Fixtures preserve raw decimals and the
em-dash cache-write cells used by the explicit no-additional-fee policy.

`gpt-56-terra-govcloud.md` and `gpt-56-luna-govcloud.md` preserve the complete
Pricing sections retrieved from those same AWS model-card URLs on 2026-11-06.
They publish equal GovCloud prices in separate `In-Region (bedrock-mantle)`
and `Geo CRIS` rows. These fixtures retain the changed prices and 30-minute
cache-write heading; trailing whitespace is trimmed and the earlier source fixtures remain unchanged.
