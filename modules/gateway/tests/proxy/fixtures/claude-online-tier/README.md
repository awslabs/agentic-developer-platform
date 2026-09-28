# Controlled Claude cache-hit regression — 12 September 2026

`cache-read.sse` preserves the actual gateway response from a controlled global
Haiku 4.5 cache-hit request in us-east-1. The upstream reports standard serving
inside message_start.message.usage.service_tier, then omits it in message_delta.
The initial output count1 is provisional; the final output count is6. Actual
counts are13 uncached input,28,170 cached reads, zero writes, and6 output.

The published standard rates per1K are input0.001/output0.005/read0.0001;
(13*0.001 +28170*0.0001 +6*0.005)/1000 =0.002860 USD.
The old selector chose offline batch when the nested serving tier was missed:
its unpublished cache-read rate fell back to input0.0005, producing0.0141065
exact/0.014107 ledger USD. `historical-decision.json` preserves that saved
version-two decision unchanged, including its diagnostic hash, so replay stays
stable while only new selection is corrected. No credentials or prompt content
are included in these fixtures.
