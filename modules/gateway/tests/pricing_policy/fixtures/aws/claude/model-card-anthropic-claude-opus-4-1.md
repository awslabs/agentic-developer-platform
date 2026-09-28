

# Claude Opus 4.1
<a name="model-card-anthropic-claude-opus-4-1"></a>

## ![Orange rounded square icon with white radial loading spinner design.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/models/claude.png) Anthropic — Claude Opus 4.1
<a name="model-card-anthropic-claude-opus-4-1-header"></a>

## Model Details
<a name="model-card-anthropic-claude-opus-4-1-details"></a>

Claude Opus 4.1 is an upgrade to Anthropic's model with improved coding, reasoning, and agentic task capabilities. For more information about model development and performance, see the [model/service card](https://www-cdn.anthropic.com/9fa30625273bafdf5af82c93719d7ca606485a16.pdf).
+ **Model launch date:** Aug 05, 2025
+ **EOL no sooner than:** Aug 05, 2026
+ **Legacy period:** at least 6 months
+ **Model lifecycle policy:** [Model lifecycle (For Models Launched Prior to Sept 7 2026)](model-lifecycle-legacy.md)
+ **Model EOL date:** January 8, 2027
+ **End User License Agreements and Terms of Use:** [View](https://aws.amazon.com/legal/bedrock/third-party-models/)
+ **Model lifecycle:** Legacy
+ **Context window:** 200K tokens
+ **Max output tokens:** 32K
+ **Reasoning:** Supported
+ **Knowledge cutoff:** Mar 2025
+ **Marketplace product ID:** `prod-w3q2d6rfge4tw`


| **Input Modalities** | **Output Modalities** | 
| --- | --- | 
| ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Audio | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Embedding | 
| ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) Image | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Image | 
| ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Speech | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Speech | 
| ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) Text | ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) Text | 
| ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Video | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) Video | 

## Endpoints and APIs supported
<a name="model-card-anthropic-claude-opus-4-1-apis-endpoints"></a>

The following tables show which endpoints and APIs are supported for Claude Opus 4.1. For more information, see [APIs supported by Amazon Bedrock](apis.md) and [Endpoints supported by Amazon Bedrock](endpoints.md).

**Endpoint support**


| **Endpoint** | **Supported** | 
| --- | --- | 
| bedrock-runtime | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | 
| bedrock-mantle | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

**APIs supported on `bedrock-runtime` endpoint**


| **Messages** | **Responses** | **Chat Completions** | **Converse** | **Invoke** | 
| --- | --- | --- | --- | --- | 
| ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | 

**APIs supported on `bedrock-mantle` endpoint**


| **Messages** | **Responses** | **Chat Completions** | **Converse** | **Invoke** | 
| --- | --- | --- | --- | --- | 
| ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

**Tip**  
Whenever possible, we recommend using the `bedrock-runtime` endpoint for new applications. See [Endpoints supported by Amazon Bedrock](endpoints.md) for details.

## Capabilities and Features
<a name="model-card-anthropic-claude-opus-4-1-capabilities"></a>

***Bedrock Features***

**Features supported using `bedrock-runtime` endpoint**


| **Supported** | **Not Supported** | 
| --- | --- | 
|  + ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Response streaming](/bedrock/latest/APIReference/API_runtime_InvokeModelWithResponseStream.html)<br />+ ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Abuse detection](abuse-detection.html)<br />+ ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Guardrails](guardrails.html)<br />+ ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Count tokens](count-tokens.html)<br />+ ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) [Computer use](computer-use.html)  |  + ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Intelligent prompt routing](prompt-routing.html)<br />+ ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Prompt optimization](prompt-management-optimize.html)<br />+ ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Knowledge base](knowledge-base.html)<br />+ ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Model evaluation](evaluation.html)<br />+ ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Prompt management](prompt-management.html)<br />+ ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Flows](flows.html)<br />+ ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) [Agents](agents.html)  | 

**Implicit and Explicit Prompt Caching using `bedrock-runtime` endpoint**

For more information, see [Prompt caching for faster model inference](prompt-caching.html).


| **Explicit Prompt Caching supported** | **Min tokens per cache checkpoint** | **Max cache checkpoints per request** | **Supported TTL** | **Fields that accept prompt cache checkpoints** | 
| --- | --- | --- | --- | --- | 
| Yes | 1,024 | 4 | 5 minutes | system, messages, and tools | 

**Computer use using `bedrock-runtime` endpoint**

For more information, see [Computer use](computer-use.html).


| **Tool type** | **Beta header** | 
| --- | --- | 
| computer\_20250124 | computer-use-2025-01-24 | 

## Pricing
<a name="model-card-anthropic-claude-opus-4-1-pricing"></a>

This model is a third-party model offered and billed through AWS Marketplace. Charges appear on your AWS bill and in AWS Cost Explorer under the model provider (not under Amazon Bedrock). For pricing, see the [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/) page.

## Programmatic Access
<a name="model-card-anthropic-claude-opus-4-1-programmatic-access"></a>

Use the following model IDs and endpoint URLs to access this model programmatically. For more information about the available APIs and endpoints, see [APIs supported](apis.html) and [Endpoints supported](endpoints.html).


| **Endpoint** | **Model ID** | **In-Region endpoint URL** | **Geo inference ID** | **Global inference ID** | 
| --- | --- | --- | --- | --- | 
| bedrock-runtime | anthropic.claude-opus-4-1-20250805-v1:0 | N/A | us.anthropic.claude-opus-4-1-20250805-v1:0 | N/A | 

*For example, if region is us-east-1 (N. Virginia), then the bedrock-runtime endpoint URL will be "https://bedrock-runtime.us-east-1.amazonaws.com".*

## Service Tiers
<a name="model-card-anthropic-claude-opus-4-1-tiers"></a>

Amazon Bedrock offers multiple service tiers to match your workload requirements. **Standard** provides pay-per-token access with no commitment (set `"service_tier": "default"` or omit the field). **Priority** delivers the fastest response times for a price premium (set `"service_tier": "priority"`). **Flex** provides lower-cost access for flexible, non-time-sensitive workloads (set `"service_tier": "flex"`). **Reserved** provides dedicated throughput with a term commitment for predictable workloads; it is set at the account level rather than per request (contact your AWS account team to enable). For more information, see [service tiers](service-tiers-inference.html).


| **Standard** | **Priority** | **Flex** | **Reserved** | 
| --- | --- | --- | --- | 
| ![Green circle with white checkmark icon.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![Red circle with white X icon indicating error, cancel, or close action.](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

## Regional Availability
<a name="model-card-anthropic-claude-opus-4-1-regional-availability"></a>

***Regional availability at a glance***

Amazon Bedrock offers three inference options: **In-Region** keeps requests within a single Region for strict compliance, **Geo Cross-Region** routes across Regions within a geography (such as US, EU, and APAC) while respecting data residency, and **Global Cross-Region** routes anywhere worldwide when there are no residency constraints. Refer to the [Regional availability by models](models-region-compatibility.md) page for more details.

Availability differs by endpoint.

**Availability using the `bedrock-runtime` endpoint**


| **Region** | **In-Region** | **Geo** | **Global** | 
| --- | --- | --- | --- | 
| us-east-1 (N. Virginia) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 
| us-east-2 (Ohio) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 
| us-west-2 (Oregon) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | ![supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-yes.png) | ![not-supported](https://docs.aws.amazon.com/bedrock/latest/userguide/images/icons/icon-no.png) | 

***Geo inference details***

**Geo: US**

Geo Inference ID: `us.anthropic.claude-opus-4-1-20250805-v1:0`


| **Source Region** | **Destination Regions** | 
| --- | --- | 
| us-east-1 (N. Virginia) | us-east-1 (N. Virginia), us-east-2 (Ohio), us-west-2 (Oregon) | 
| us-east-2 (Ohio) | us-east-1 (N. Virginia), us-east-2 (Ohio), us-west-2 (Oregon) | 
| us-west-2 (Oregon) | us-east-1 (N. Virginia), us-east-2 (Ohio), us-west-2 (Oregon) | 

## Quotas and Limits
<a name="model-card-anthropic-claude-opus-4-1-quotas"></a>

Your AWS account has default quotas to maintain the performance of the service and to ensure appropriate usage of Amazon Bedrock. The default quotas assigned to an account might be updated depending on regional factors, payment history, fraudulent usage, and/or approval of a quota [increase request](quotas-increase.html). For more information, see [Quotas for Amazon Bedrock](quotas.md) documentation and see the [limits](/general/latest/gr/bedrock.html#limits_bedrock) for the model.

## Sample Code
<a name="model-card-anthropic-claude-opus-4-1-sample-code"></a>

**Step 1 - AWS Account:** If you have an AWS account already, skip this step. If you are new to AWS, sign up for an [AWS account](https://portal.aws.amazon.com/billing/signup).

**Step 2 - API key:** Go to the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/home#/api-keys/long-term/create) and generate a long-term API key.

**Step 3 - Get the SDK:** To use this getting started guide, you must have Python already installed. Then install the relevant software depending on the APIs you are using.

```
pip install boto3
```

**Step 4 - Set environment variables:** Configure your environment to use the API key for authentication.

```
AWS_BEARER_TOKEN_BEDROCK="<provide your Bedrock API key>"
```

**Step 5 - Run your first inference request:** Save the file as `bedrock-first-request.py`

------
#### [ Invoke API ]

```
import json
import boto3

client = boto3.client('bedrock-runtime', region_name='us-east-1')
response = client.invoke_model(
    modelId='anthropic.claude-opus-4-1-20250805-v1:0',
    body=json.dumps({
            'anthropic_version': 'bedrock-2023-05-31',
            'messages': [{ 'role': 'user', 'content': 'Can you explain the features of Amazon Bedrock?'}],
            'max_tokens': 1024
    })
)
print(json.loads(response['body'].read()))
```

------
#### [ Converse API ]

```
import boto3

client = boto3.client('bedrock-runtime', region_name='us-east-1')
response = client.converse(
    modelId='anthropic.claude-opus-4-1-20250805-v1:0',
    messages=[
        {
            'role': 'user',
            'content': [{'text': 'Can you explain the features of Amazon Bedrock?'}]
        }
    ]
)
print(response)
```

------