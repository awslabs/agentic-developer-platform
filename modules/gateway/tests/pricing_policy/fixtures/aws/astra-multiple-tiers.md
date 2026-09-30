## Pricing
<a name="model-card-openai-gpt-6-astra-pricing"></a>

All prices are in USD per 1 million tokens. The following tables list Standard and Ultrafast prices.

Commercial In-Region and Geo CRIS prices include a 10% premium over the corresponding OpenAI rates for the same service tier. You do not need to add this premium.

*Priority and Flex tiers are not supported for this model.*

### Standard — Commercial Regions, short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-6-astra-pricing-commercial-short"></a>


| **Inference option** | **Input** | **Input — 30m cache write** | **Input — cache read** | **Output** | 
| --- | --- | --- | --- | --- | 
| In-Region | $11.00 | $13.75 | $1.10 | $55.00 | 
| Geo CRIS | $11.00 | $13.75 | $1.10 | $55.00 | 
| Global CRIS | $10.00 | $12.50 | $1.00 | $50.00 | 

### Standard — Commercial Regions, long context (more than 272K input tokens)
<a name="model-card-openai-gpt-6-astra-pricing-commercial-long"></a>


| **Inference option** | **Input** | **Input — 30m cache write** | **Input — cache read** | **Output** | 
| --- | --- | --- | --- | --- | 
| In-Region | $22.00 | $27.50 | $2.20 | $82.50 | 
| Geo CRIS | $22.00 | $27.50 | $2.20 | $82.50 | 
| Global CRIS | $20.00 | $25.00 | $2.00 | $75.00 | 

Ultrafast prices are six times the corresponding Standard prices. Ultrafast is available only through `bedrock-mantle` in `us-east-1` and US CRIS on `bedrock-runtime`. Short-context prices apply to requests with at most 272,000 input tokens. When input exceeds this threshold, long-context prices apply to the entire request.

The Global CRIS rates below are included as a pricing reference. The supported Ultrafast routes are regional Mantle in `us-east-1` and US CRIS on `bedrock-runtime`.

### Ultrafast — Commercial Regions, short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-6-astra-pricing-ultrafast-short"></a>


| **Inference option** | **Input** | **Input — 30m cache write** | **Input — cache read** | **Output** | 
| --- | --- | --- | --- | --- | 
| In-Region (us-east-1) | $66.00 | $82.50 | $6.60 | $330.00 | 
| Geo CRIS (US) | $66.00 | $82.50 | $6.60 | $330.00 | 
| Global CRIS (pricing reference) | $60.00 | $75.00 | $6.00 | $300.00 | 

### Ultrafast — Commercial Regions, long context (more than 272K input tokens)
<a name="model-card-openai-gpt-6-astra-pricing-ultrafast-long"></a>


| **Inference option** | **Input** | **Input — 30m cache write** | **Input — cache read** | **Output** | 
| --- | --- | --- | --- | --- | 
| In-Region (us-east-1) | $132.00 | $165.00 | $13.20 | $495.00 | 
| Geo CRIS (US) | $132.00 | $165.00 | $13.20 | $495.00 | 
| Global CRIS (pricing reference) | $120.00 | $150.00 | $12.00 | $450.00 | 

