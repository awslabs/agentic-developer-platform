# AWS Bedrock model card pricing excerpt

## Pricing
<a name="model-card-openai-gpt-56-terra-pricing"></a>

All prices are in USD per 1 million tokens for the Standard tier.

The **Input — 30-minute cache write** column is the price per 1 million tokens written to the prompt cache. The 30-minute figure is how long a cached prompt prefix is retained, not a quantity of tokens. For more information, see [prompt caching](prompt-caching.md).

Commercial In-Region prices include a 10% fee over OpenAI rates. You do not need to add this fee.

*Priority and Flex tiers are not supported for this model.*

### Commercial Regions — short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-56-terra-pricing-commercial-short"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region | $2.20 | $2.75 | $0.22 | $13.20 |
| Geo CRIS | $2.20 | $2.75 | $0.22 | $13.20 |
| Global CRIS | $2.00 | $2.50 | $0.20 | $12.00 |

### Commercial Regions — long context (more than 272K input tokens)
<a name="model-card-openai-gpt-56-terra-pricing-commercial-long"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region | $4.40 | $5.50 | $0.44 | $19.80 |
| Geo CRIS | $4.40 | $5.50 | $0.44 | $19.80 |
| Global CRIS | $4.00 | $5.00 | $0.40 | $18.00 |

### AWS GovCloud (US-East and US-West)
<a name="model-card-openai-gpt-56-terra-pricing-govcloud"></a>

#### Short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-56-terra-pricing-govcloud-short"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region (bedrock-mantle) | $2.70 | $3.375 | $0.27 | $16.20 |
| Geo CRIS | $2.70 | $3.375 | $0.27 | $16.20 |

#### Long context (more than 272K input tokens)
<a name="model-card-openai-gpt-56-terra-pricing-govcloud-long"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region (bedrock-mantle) | $5.40 | $6.75 | $0.54 | $24.30 |
| Geo CRIS | $5.40 | $6.75 | $0.54 | $24.30 |
