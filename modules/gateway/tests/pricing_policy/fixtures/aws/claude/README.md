# AWS Claude source evidence — 12 September 2026

`pricing-widgets.html` preserves the exact five Claude pricing widget
`data-pricing-markup` values from https://aws.amazon.com/bedrock/pricing/.
Only the outer page wrapper and non-Claude widgets were removed; reserved and latency-optimized Claude tables remain to exercise safe exclusion.
`token-map.json` preserves the real manifest and map entries referenced by those
widgets, for every published region, from
https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrockfoundationmodels/USD/current/bedrockfoundationmodels.json.
Unreferenced map tokens and `sets` were removed; no values were invented.
The immutable 2026-09-12.2 snapshot records the SHA256 of the complete original
page and decoded map, and their composite digest; minimized fixtures necessarily
have different byte hashes.

`expected-rates.json` was produced by an independent standalone source inventory
that imports no application pricing code. It intersects 18 current complete AWS
model cards (also preserved here, with fetch-time/hash manifest) with exact widget
scope/region/map tokens. It covers 1,006 rate keys: 658 standard and 348 explicitly
published batch prices. Batch prices do not imply an online service-tier API.
Units are USD per 1M in the widget, converted exactly to USD per 1K using Decimal.
All token dimensions use published values, including 1h cache write and the
2.5%-of-input cache-read prices on Fable 5.1 and Mythos 5.1.

Twelve model/endpoint/inference availability variants have no exact map prices;
they are listed in snapshot provenance and remain estimated. Priority/Flex are
unsupported on current cards. Reserved hourly/TPM rates and the latency-optimized
Claude 3.5 Haiku widget are excluded: its header says per1K while token values
are1/5, and the policy lacks a latency-mode dimension. Legacy 3.5 Sonnet static
extended-access pricing differs from normal widgets; no current card exists in
this audited inventory, so those compatibility policies remain estimated.
No cache ratio or geography multiplier is inferred.
