# AWS Bedrock model card pricing excerpt

## Pricing
<a name="model-card-openai-gpt-56-luna-pricing"></a>

All prices are in USD per 1 million tokens for the Standard tier.

The **Input — 30-minute cache write** column is the price per 1 million tokens written to the prompt cache. The 30-minute figure is how long a cached prompt prefix is retained, not a quantity of tokens. For more information, see [prompt caching](prompt-caching.md).

Commercial In-Region prices include a 10% fee over OpenAI rates. You do not need to add this fee.

*Priority and Flex tiers are not supported for this model.*

### Commercial Regions — short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-56-luna-pricing-commercial-short"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region | $0.22 | $0.275 | $0.022 | $1.32 |
| Geo CRIS | $0.22 | $0.275 | $0.022 | $1.32 |
| Global CRIS | $0.20 | $0.25 | $0.02 | $1.20 |

### Commercial Regions — long context (more than 272K input tokens)
<a name="model-card-openai-gpt-56-luna-pricing-commercial-long"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region | $0.44 | $0.55 | $0.044 | $1.98 |
| Geo CRIS | $0.44 | $0.55 | $0.044 | $1.98 |
| Global CRIS | $0.40 | $0.50 | $0.04 | $1.80 |

### AWS GovCloud (US-East and US-West)
<a name="model-card-openai-gpt-56-luna-pricing-govcloud"></a>

#### Short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-56-luna-pricing-govcloud-short"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region (bedrock-mantle) | $0.27 | $0.3375 | $0.027 | $1.62 |
| Geo CRIS | $0.27 | $0.3375 | $0.027 | $1.62 |

#### Long context (more than 272K input tokens)
<a name="model-card-openai-gpt-56-luna-pricing-govcloud-long"></a>


| **Inference option** | **Input** | **Input — 30-minute cache write** | **Input — cache read** | **Output** |
| --- | --- | --- | --- | --- |
| In-Region (bedrock-mantle) | $0.54 | $0.675 | $0.054 | $2.43 |
| Geo CRIS | $0.54 | $0.675 | $0.054 | $2.43 |
