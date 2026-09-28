# Real AWS pricing source fixtures (#4969 / #4976)

## `bulk_catalog_us-east-1_20260911124408.json`

A **real** AWS Bedrock bulk-pricing catalog, retrieved unmodified from

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/20260911124408/us-east-1/index.json

This is the exact `catalog_version` the frozen snapshot `2026-09-12.1` records in
its provenance. It is sanitized only by *subsetting*: all 64 OpenAI products are
kept verbatim along with their `terms.OnDemand` entries, plus 12 non-OpenAI
products so the parser must still prove it ignores them. No attribute value and
no price was edited.

The design note (§4.5) requires real fixtures because the retired parser was
written against a synthetic one and consequently accepted **nothing** in
production. This file reproduces all three reasons, which a synthetic fixture
cannot:

- **No `attributes.modelId` exists on any product.** The old parser keyed on it.
- **`operation` is the empty string** on every product, so it cannot be a join key.
- **Input and output are separate SKUs**, so a price needs a join across products
  rather than one lookup.

It also carries the two duplicate SKU families the parser has to reconcile — the
modern `service_tier` family and the legacy `feature` ("On-demand Inference" /
"Batch Inference") family — which agree on price here. Rejecting a *conflict*
between them is required behaviour; deduping an *agreement* is required too.

Finally it pins the fact that Safeguard tier rates must be read, never derived:
the published "Priority +75%" multiplier holds for gpt-oss-20b/120b but the
Safeguard SKUs are rounded to whole cents (`0.00012`, not `0.0001225`).

## Model cards

Not included. The eight frontier model cards were retrieved and verified against
live AWS publications on 2026-09-12 during the investigation (design note §11),
but `https://aws.amazon.com/bedrock/model-cards/<model>.md` returns HTTP 404 from
the build environment now, so no card could be captured as a fixture here without
fabricating it — and a fabricated card fixture is precisely the failure mode that
produced the parser this change replaces.

Card-parser tests therefore use Markdown built to the structure the design note
records (§4.5: one `## Pricing` section, bold scope labels rather than nested
headings, GovCloud blocks following commercial tables with repeated context
headings). Those fixtures test the state machine's handling of that structure;
they are NOT evidence of AWS's current card content. The rates they assert are
the §10 inventory, which the catalog fixture corroborates for GPT-OSS.

**Release verification must diff the parser against live cards** before the first
real publication is trusted. See the PR's remaining-verification section.
