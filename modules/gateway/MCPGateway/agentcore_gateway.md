# Amazon Bedrock AgentCore Gateway — MCP Gateway Reference

> Source: [Transform your MCP architecture: Unite MCP servers through AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/transform-your-mcp-architecture-unite-mcp-servers-through-agentcore-gateway/) — AWS Machine Learning Blog, November 6, 2025. Authors: Frank Dallezotte, Dhawalkumar Patel, Ganesh Thiyagarajan.

---

## What Is AgentCore Gateway?

Amazon Bedrock AgentCore Gateway is a fully managed AWS service that acts as a centralized MCP tool server. It provides a unified interface where AI agents can discover, access, and invoke tools from multiple backend sources — including MCP servers, REST APIs (OpenAPI), AWS Lambda functions, and Smithy models — through a single managed endpoint.

The key capability: existing MCP servers can be added as a "target type" in the gateway, allowing organizations to group multiple task-specific MCP servers behind one gateway interface. This eliminates the need for agents to manage separate connections, authentication contexts, and discovery flows for each MCP server.

---

## Problem Statement

As AI agents scale, teams create dozens to hundreds of specialized MCP servers per domain, team, or use case. Without a centralized approach:

- Tool discovery and sharing across the organization becomes fragmented
- Managing authentication across multiple MCP servers grows complex
- Maintaining separate gateway instances per server is operationally unmanageable
- Agents must handle multiple connections and protocol variations

---

## Architecture Overview

### Core Concept: Targets

In AgentCore Gateway, a **target** defines the backend that the gateway exposes as tools to agents. Supported target types:

| Target Type | Description |
|---|---|
| MCP Server | Existing MCP servers (custom, public, or open source) |
| AWS Lambda | Serverless functions |
| OpenAPI Spec | REST APIs described via OpenAPI |
| Smithy Model | AWS service APIs |
| AgentCore Runtime | Agents exposed as tools |
| Another AgentCore Gateway | Federation — hierarchical tool organization across org boundaries |

### How It Works

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│  AI Agent    │────▶│  AgentCore Gateway   │────▶│  MCP Server A       │
│  (Strands,   │     │  (Single Endpoint)   │────▶│  MCP Server B       │
│   any MCP    │     │                      │────▶│  Lambda Function    │
│   client)    │     │  - Auth (JWT/OAuth)  │────▶│  REST API (OpenAPI) │
│              │     │  - Tool Discovery    │     └─────────────────────┘
│              │     │  - Semantic Search   │
│              │     │  - Tool Invocation   │
└─────────────┘     └──────────────────────┘
```

The gateway sits between agents and all backend tool providers. Agents connect to one URL, authenticate once, and get a unified view of all tools regardless of their backend implementation.

### E-Commerce Example

An ordering agent needs tools from three teams:
- **Shopping Cart team** → cart management MCP server
- **Product Catalog team** → product browsing/search MCP server
- **Promotions team** → promotional logic MCP server

Without the gateway: the agent manages 3 separate connections with 3 auth contexts.
With AgentCore Gateway: one connection, one auth flow, unified tool catalog.

---

## Key Capabilities

### 1. Unified Tool Discovery

When an agent calls `ListTools`, the gateway returns a merged catalog from all registered targets. Tools from MCP servers appear alongside Lambda functions and REST APIs. The gateway uses a **cache-first approach** — tool definitions are retrieved from persistent storage (populated during synchronization), not via real-time calls to MCP servers.

### 2. Semantic Tool Search

The gateway automatically generates embeddings for each tool's name, description, and parameter descriptions during synchronization. Agents can discover tools via semantic search (intent-based) rather than exact keyword matching.

```python
# Search for tools semantically through the gateway
payload = {
    "jsonrpc": "2.0",
    "id": "search-tools-request",
    "method": "tools/call",
    "params": {
        "name": "x_amz_bedrock_agentcore_search",
        "arguments": {
            "query": "order operations"
        }
    }
}
response = requests.post(gateway_url, headers=headers, json=payload)
```

### 3. Tool Namespace Prefixing

The gateway adds target-specific prefixes to tool names to prevent naming collisions across targets. For example, if two MCP servers both expose a `getStatus` tool, they become `cart_getStatus` and `catalog_getStatus`.

### 4. Centralized Authentication

The gateway decouples inbound auth (agent → gateway) from outbound auth (gateway → MCP server):

- **Inbound**: JWT-based authorization via Amazon Cognito or any OAuth 2.0-compliant provider
- **Outbound**: Per-target OAuth credential providers managed via AgentCore Identity service

This means agents authenticate once against the gateway, and the gateway handles the complexity of authenticating against each backend MCP server using its configured identity provider.

### 5. Tool Synchronization

Two synchronization modes keep tool definitions current:

**Implicit sync** — occurs automatically during `CreateGatewayTarget` and `UpdateGatewayTarget`. Ensures targets in READY state are immediately usable with valid tool definitions.

**Explicit sync** — via the `SynchronizeGatewayTargets` API. Provides on-demand refresh after deploying new tools or updating existing ones. Processes tools in batches of 100, normalizes definitions, and adds target prefixes.

Synchronization flow:
1. Gateway obtains OAuth token from AgentCore Identity
2. Initializes session with MCP server (protocol handshake)
3. Makes paginated `tools/list` calls (batches of 100)
4. Normalizes tool definitions and generates search embeddings
5. Stores in persistent cache

### 6. Tool Invocation (tools/call)

Unlike the cached `ListTools`, `tools/call` requires real-time communication with the MCP server:

1. Gateway validates the tool exists in synchronized definitions
2. Retrieves fresh OAuth credentials from AgentCore Identity
3. Initializes session with the target MCP server
4. Executes the tool call and returns the result

### 7. Gateway Federation

One AgentCore Gateway instance can serve as a target for another gateway, enabling hierarchical tool organization across organizational boundaries.

---

## Implementation Walkthrough

### Prerequisites

- AWS account with Amazon Bedrock AgentCore access
- Python 3.12+
- OAuth 2.0 understanding
- IAM role with gateway permissions

### Step 1: Create the Gateway

```python
import boto3

gateway_client = boto3.client("bedrock-agentcore-control")

auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": ["<cognito_client_id>"],
        "discoveryUrl": "<cognito_oauth_discovery_url>",
    }
}

create_response = gateway_client.create_gateway(
    name="DemoGateway",
    roleArn="<IAM_Role_ARN>",
    protocolType="MCP",
    authorizerType="CUSTOM_JWT",
    authorizerConfiguration=auth_config,
    description="AgentCore Gateway with MCP Server Target",
)

gateway_id = create_response["gatewayId"]
gateway_url = create_response["gatewayUrl"]
```

### Step 2: Create a Sample MCP Server

Uses FastMCP with `stateless_http=True` (required for AgentCore Runtime compatibility):

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

@mcp.tool()
def getOrder() -> int:
    """Get an order"""
    return 123

@mcp.tool()
def updateOrder(orderId: int) -> int:
    """Update existing order"""
    return 456

@mcp.tool()
def cancelOrder(orderId: int) -> int:
    """Cancel existing order"""
    return 789

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

### Step 3: Deploy to AgentCore Runtime

```python
from bedrock_agentcore_starter_toolkit import Runtime

agentcore_runtime = Runtime()

auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": ["<runtime_cognito_client_id>"],
        "discoveryUrl": "<cognito_oauth_discovery_url>",
    }
}

agentcore_runtime.configure(
    entrypoint="mcp_server.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region=region,
    authorizer_configuration=auth_config,
    protocol="MCP",
    agent_name="mcp_server_agentcore",
)

launch_result = agentcore_runtime.launch()
agent_arn = launch_result.agent_arn

# Construct the MCP server URL
encoded_arn = agent_arn.replace(":", "%3A").replace("/", "%2F")
mcp_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
```

### Step 4: Create OAuth Credential Provider

```python
identity_client = boto3.client("bedrock-agentcore-control", region_name=region)

cognito_provider = identity_client.create_oauth2_credential_provider(
    name="gateway-mcp-server-identity",
    credentialProviderVendor="CustomOauth2",
    oauth2ProviderConfigInput={
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {
                "discoveryUrl": "<cognito_oauth_discovery_url>",
            },
            "clientId": "<runtime_cognito_client_id>",
            "clientSecret": "<cognito_client_secret>",
        }
    },
)
cognito_provider_arn = cognito_provider["credentialProviderArn"]
```

### Step 5: Add MCP Server as Gateway Target

```python
create_target_response = gateway_client.create_gateway_target(
    name="mcp-server-target",
    gatewayIdentifier=gateway_id,
    targetConfiguration={"mcp": {"mcpServer": {"endpoint": mcp_url}}},
    credentialProviderConfigurations=[
        {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {
                "oauthCredentialProvider": {
                    "providerArn": cognito_provider_arn,
                    "scopes": ["<cognito_oauth_scopes>"],
                }
            },
        },
    ],
)
target_id = create_target_response["targetId"]

# Poll until READY
import time
while True:
    resp = gateway_client.get_gateway_target(
        gatewayIdentifier=gateway_id, targetId=target_id
    )
    if resp["status"] == "READY":
        break
    time.sleep(5)
```

### Step 6: Test with Strands Agents

```python
from strands import Agent
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

def create_transport():
    return streamablehttp_client(
        gateway_url, headers={"Authorization": f"Bearer {token}"}
    )

client = MCPClient(create_transport)

with client:
    tools = client.list_tools_sync()
    agent = Agent(model=your_model, tools=tools)
    agent("Hi, can you list all tools available to you")
    agent("Get the Order id")
```

---

## Key APIs

| API | Purpose |
|-----|---------|
| `CreateGateway` | Create a new gateway with auth config |
| `CreateGatewayTarget` | Add an MCP server (or other target) to the gateway |
| `UpdateGatewayTarget` | Update target configuration |
| `GetGatewayTarget` | Check target status |
| `SynchronizeGatewayTargets` | On-demand refresh of tool definitions from MCP servers |
| `ListTools` (MCP) | Returns cached, unified tool catalog |
| `tools/call` (MCP) | Real-time tool invocation through the gateway |
| `x_amz_bedrock_agentcore_search` | Semantic search across all tools |
| `CreateOAuth2CredentialProvider` | Configure outbound auth credentials |

---

## Organizational Patterns

Teams can group MCP servers behind the gateway based on:

- **Business unit alignment** — organize by department or business unit
- **Product feature boundaries** — each product team owns their MCP server with domain-specific tools
- **Security and access control** — different MCP servers with different auth mechanisms, unified by the gateway

---

## References

- [AgentCore Gateway Developer Guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html)
- [GitHub Code Samples — MCP Server as Target](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/02-AgentCore-gateway/05-mcp-server-as-a-target)
- [Jupyter Notebook Walkthrough](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/02-AgentCore-gateway/05-mcp-server-as-a-target/01-mcp-server-target.ipynb)
- [AgentCore Gateway Inbound Auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html)
- [AgentCore Gateway Pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [AgentCore Starter Toolkit Quickstart](https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/gateway/quickstart.html)
- [SynchronizeGatewayTargets API Reference](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_SynchronizeGatewayTargets.html)
- [Strands Agents Framework](https://strandsagents.com/latest/)

> Content was rephrased for compliance with licensing restrictions. Original source: AWS Machine Learning Blog.
