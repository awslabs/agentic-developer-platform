

# GPT-6 Luna
<a name="model-card-openai-gpt-6-luna"></a>

## ![OpenAI logo](https://docs.aws.amazon.com/bedrock/latest/userguide/images/models/openai.png) OpenAI — GPT-6 Luna
<a name="model-card-openai-gpt-6-luna-header"></a>

## Model details
<a name="model-card-openai-gpt-6-luna-details"></a>

GPT-6 Luna is designed for repeatable work at scale. It can summarize documents, extract information, and answer focused questions. It accepts text and images and returns text, including code. You can adjust its reasoning effort to fit your workload.
+ **Model launch date:** September 22, 2026
+ **EOL no sooner than:** September 22, 2027
+ **Legacy period:** at least 6 months
+ **Model lifecycle policy:** [Model lifecycle](model-lifecycle.md)
+ **Model EOL date:** N/A
+ **End User License Agreements and Terms of Use:** [View](https://aws.amazon.com/legal/bedrock/third-party-models/)
+ **Model lifecycle:** Active
+ **Context window:** 1,050,000 tokens
+ **Marketplace product ID:** `prod-fiwlckcpwkwli`


| **Input modalities** | **Output modalities** | 
| --- | --- | 
| ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Audio | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Embedding | 
| ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) Image | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Image | 
| ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Speech | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Speech | 
| ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) Text | ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) Text | 
| ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Video | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Video | 

## Endpoints and APIs supported
<a name="model-card-openai-gpt-6-luna-apis-endpoints"></a>

These tables show the endpoints and APIs that GPT-6 Luna supports. See [APIs supported by Amazon Bedrock](apis.md) and [Endpoints supported by Amazon Bedrock](endpoints.md).

**Endpoint support**


| **Endpoint** | **Supported** | 
| --- | --- | 
| bedrock-runtime | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | 
| bedrock-mantle | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | 

**APIs supported on the `bedrock-runtime` endpoint**


| **Messages** | **Responses** | **Chat Completions** | **Converse** | **Invoke** | 
| --- | --- | --- | --- | --- | 
| ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

**APIs supported on the `bedrock-mantle` endpoint**


| **Messages** | **Responses** | **Chat Completions** | **Converse** | **Invoke** | 
| --- | --- | --- | --- | --- | 
| ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

**Note**  
On `bedrock-mantle`, both APIs use the `/openai/v1` base path. Do not use `/v1`. Use either API with this model:  
For Responses, use `/openai/v1/responses`.
For Chat Completions, use `/openai/v1/chat/completions`.

**Tip**  
For new applications, use the `bedrock-runtime` endpoint when possible. See [Endpoints supported by Amazon Bedrock](endpoints.md) for details.

## Capabilities and features
<a name="model-card-openai-gpt-6-luna-capabilities"></a>

***Amazon Bedrock features***

**Features supported on the `bedrock-runtime` endpoint**


| **Supported** | **Not Supported** | 
| --- | --- | 
|  + ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Projects (default project only)](projects.html)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Invocation logs](model-invocation-logging.html)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Response streaming](/bedrock/latest/APIReference/API_runtime_InvokeModelWithResponseStream.html)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Abuse detection](abuse-detection.html)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Guardrails](guardrails.html) ([Converse API](conversation-inference.html) only)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Application inference profiles](cost-mgmt-application-inference-profiles.html) ([Converse API](conversation-inference.html) only; not supported with Responses or Chat Completions APIs)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Implicit Prompt Caching](prompt-caching.html#prompt-caching-implicit) (Responses API only)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Explicit Prompt Caching](prompt-caching.html#prompt-caching-explicit) (Responses API only)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Structured outputs](structured-output.html)  |  + ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Server-side tool use](tool-use.html)<br />+ ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Intelligent prompt routing](prompt-routing.html)<br />+ ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Count tokens](count-tokens.html)  | 

**Features supported on the `bedrock-mantle` endpoint**


| **Supported** | **Not Supported** | 
| --- | --- | 
|  + ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Server-side tool calling](tool-use.html)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Projects](projects.html)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Implicit Prompt Caching](prompt-caching.html#prompt-caching-implicit) (Responses API only)<br />+ ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Explicit Prompt Caching](prompt-caching.html#prompt-caching-explicit) (Responses API only)  |  + ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Application inference profiles](cost-mgmt-application-inference-profiles.html)  | 

**Reasoning effort**

Set reasoning effort to `none`, `low`, `medium`, `high`, `xhigh`, or `max`. The default is `medium`.

## Pricing
<a name="model-card-openai-gpt-6-luna-pricing"></a>

All prices are in USD per 1 million tokens for the Standard tier.

Mantle in-Region and US geographic cross-Region inference include a 10% premium. The base rates are OpenAI first-party Standard rates. Global cross-Region inference uses those rates with no premium. The prices below already include any premium.

Priority and Flex tiers are not supported for these inference options.

### Commercial Regions — short context (272K input tokens or fewer)
<a name="model-card-openai-gpt-6-luna-pricing-commercial-short"></a>


| **Inference option** | **Input** | **Input — cache write** | **Input — cache read** | **Output** | 
| --- | --- | --- | --- | --- | 
| Mantle in-Region | $0.11 | $0.1375 | $0.011 | $0.55 | 
| US Geo CRIS | $0.11 | $0.1375 | $0.011 | $0.55 | 
| Global CRIS | $0.10 | $0.125 | $0.01 | $0.50 | 

### Commercial Regions — long context (more than 272K input tokens)
<a name="model-card-openai-gpt-6-luna-pricing-commercial-long"></a>


| **Inference option** | **Input** | **Input — cache write** | **Input — cache read** | **Output** | 
| --- | --- | --- | --- | --- | 
| Mantle in-Region | $0.22 | $0.275 | $0.022 | $0.825 | 
| US Geo CRIS | $0.22 | $0.275 | $0.022 | $0.825 | 
| Global CRIS | $0.20 | $0.25 | $0.02 | $0.75 | 

Long-context rates apply to the full request when input exceeds 272,000 tokens.

**Note**  
Prices are subject to change. For current prices, see [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).

## Call the model
<a name="model-card-openai-gpt-6-luna-programmatic-access"></a>

Use these model IDs and endpoint URLs to call the model. See [APIs supported](apis.html) and [Endpoints supported](endpoints.html).


| **Endpoint** | **Model ID** | **In-Region endpoint URL** | **Geo inference ID** | **Global inference ID** | 
| --- | --- | --- | --- | --- | 
| bedrock-mantle | openai.gpt-6-luna | https://bedrock-mantle.{region}.api.aws/openai/v1 | Not supported | Not supported | 
| bedrock-runtime | openai.gpt-6-luna | Not supported | us.openai.gpt-6-luna | global.openai.gpt-6-luna | 

On `bedrock-runtime`:
+ Use this base URL: `https://bedrock-runtime.{region}.amazonaws.com/openai/v1`.
+ Set the model ID to `us.openai.gpt-6-luna` or `global.openai.gpt-6-luna`.
+ Choose a profile that is available in your source Region.
+ You cannot use the base model ID for in-Region calls on this endpoint.

On `bedrock-mantle`, use `openai.gpt-6-luna` with the `/openai/v1` base path. Use the US East (N. Virginia) (`us-east-1`) Region for this model on this endpoint.

## Service tiers
<a name="model-card-openai-gpt-6-luna-tiers"></a>

This model supports only the **Standard** tier in Amazon Bedrock. You pay per token with no commitment. Set `"service_tier": "default"` or omit the field. Priority, Flex, and Reserved are not supported. For more information, see [service tiers](service-tiers-inference.html).


| **Standard** | **Priority** | **Flex** | **Reserved** | 
| --- | --- | --- | --- | 
| ![Supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![Not supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

## Supported Regions
<a name="model-card-openai-gpt-6-luna-regional-availability"></a>

Each endpoint supports a different set of Regions. See [Regional availability by models](models-region-compatibility.md).

**The `bedrock-mantle` endpoint**


| **Region** | **In-Region** | **Geo** | **Global** | 
| --- | --- | --- | --- | 
| us-east-1 (US East (N. Virginia)) | Supported | Not supported | Not supported | 

**The `bedrock-runtime` endpoint**


| **Source Region** | **In-Region** | **US Geo CRIS** | **Global CRIS** | 
| --- | --- | --- | --- | 
| us-east-1 | Not supported | Supported | Supported | 
| us-east-2 | Not supported | Supported | Supported | 
| us-west-1 | Not supported | Supported | Supported | 
| us-west-2 | Not supported | Supported | Supported | 
| ca-central-1 | Not supported | Supported | Supported | 
| ca-west-1 | Not supported | Supported | Supported | 
| eu-central-1 | Not supported | Not supported | Supported | 
| eu-central-2 | Not supported | Not supported | Supported | 
| eu-north-1 | Not supported | Not supported | Supported | 
| eu-south-1 | Not supported | Not supported | Supported | 
| eu-south-2 | Not supported | Not supported | Supported | 
| eu-west-1 | Not supported | Not supported | Supported | 
| eu-west-2 | Not supported | Not supported | Supported | 
| eu-west-3 | Not supported | Not supported | Supported | 
| ap-east-2 | Not supported | Not supported | Supported | 
| ap-northeast-1 | Not supported | Not supported | Supported | 
| ap-northeast-2 | Not supported | Not supported | Supported | 
| ap-northeast-3 | Not supported | Not supported | Supported | 
| ap-south-1 | Not supported | Not supported | Supported | 
| ap-south-2 | Not supported | Not supported | Supported | 
| ap-southeast-1 | Not supported | Not supported | Supported | 
| ap-southeast-2 | Not supported | Not supported | Supported | 
| ap-southeast-3 | Not supported | Not supported | Supported | 
| ap-southeast-4 | Not supported | Not supported | Supported | 
| ap-southeast-5 | Not supported | Not supported | Supported | 
| ap-southeast-6 | Not supported | Not supported | Supported | 
| ap-southeast-7 | Not supported | Not supported | Supported | 
| il-central-1 | Not supported | Not supported | Supported | 
| af-south-1 | Not supported | Not supported | Supported | 
| sa-east-1 | Not supported | Not supported | Supported | 
| mx-central-1 | Not supported | Not supported | Supported | 

## Quotas and limits
<a name="model-card-openai-gpt-6-luna-quotas"></a>

Quotas vary by account and Region. To review your quotas or request an increase, see [Quotas for Amazon Bedrock](quotas.md) and [Request an increase to a quota](quotas-increase.html).

On `bedrock-runtime`, the quota counts output tokens at a 10-to-1 rate. Each output token uses 10 tokens of quota.

## Sample code
<a name="model-card-openai-gpt-6-luna-sample-code"></a>

**Step 1 - Create an AWS account:** If you already have an AWS account, skip this step. Otherwise, sign up for an [AWS account](https://portal.aws.amazon.com/billing/signup).

**Step 2 - Create an API key:** Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/home#/api-keys/long-term/create). Create a long-term API key.

**Step 3 - Install the SDK:** You need Python to run these examples. Install the OpenAI SDK with the command below. Both APIs use this SDK.

------
#### [ OpenAI SDK ]

```
python3 -m pip install openai
```

------

### Step 4 - Set environment variables
<a name="model-card-openai-gpt-6-luna-sample-code-environment"></a>

Choose the tab for your endpoint. Set your API key and the base URL shown there.

------
#### [ bedrock-mantle ]

```
export OPENAI_API_KEY="<provide your Bedrock API key>"
export OPENAI_BASE_URL="https://bedrock-mantle.us-east-1.api.aws/openai/v1"
```

------
#### [ bedrock-runtime ]

```
export OPENAI_API_KEY="<provide your Bedrock API key>"
export OPENAI_BASE_URL="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
```

------

**Note**  
On `bedrock-runtime`, use a cross-Region inference profile as the model: `us.openai.gpt-6-luna` or `global.openai.gpt-6-luna`. This model does not support in-Region calls on this endpoint.  
Your IAM identity needs `bedrock:InvokeModel` permission for both resources:  
The inference profile.
Your AWS account's default project: `arn:aws:bedrock:{region}:{account-id}:project/default`.

### Step 5 - Send your first request
<a name="model-card-openai-gpt-6-luna-sample-code-request"></a>

Save one of the examples below as `bedrock-first-request.py`.

#### bedrock-mantle
<a name="model-card-openai-gpt-6-luna-sample-code-mantle"></a>

Use the settings from [Step 4 - Set environment variables](#model-card-openai-gpt-6-luna-sample-code-environment). Choose the `bedrock-mantle` tab.

------
#### [ Responses API ]

```
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"])

response = client.responses.create(
    model="openai.gpt-6-luna",
    input="Can you explain the features of Amazon Bedrock?"
)
print(response)
```

------
#### [ Chat Completions API ]

```
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"])

response = client.chat.completions.create(
    model="openai.gpt-6-luna",
    messages=[{"role": "user", "content": "Can you explain the features of Amazon Bedrock?"}]
)
print(response)
```

------

#### bedrock-runtime: OpenAI SDK
<a name="model-card-openai-gpt-6-luna-sample-code-runtime-openai"></a>

Use the settings from [Step 4 - Set environment variables](#model-card-openai-gpt-6-luna-sample-code-environment). Choose the `bedrock-runtime` tab. Use either API. Both examples use the US system inference profile.

------
#### [ Responses API ]

```
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"])

response = client.responses.create(
    model="us.openai.gpt-6-luna",
    input="Can you explain the features of Amazon Bedrock?"
)
print(response)
```

------
#### [ Chat Completions API ]

```
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"])
response = client.chat.completions.create(
    model="us.openai.gpt-6-luna",
    messages=[{"role": "user", "content": "Can you explain the features of Amazon Bedrock?"}]
)
print(response)
```

------