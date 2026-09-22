# AgentCore Browser boundary contract

## Caller contract

Reasoning-agent orchestration has no AgentCore Browser IAM permissions. It must
submit each URL to the trusted broker through `browser_client.analyze_url`:

```python
from browser_client import analyze_url

analysis = analyze_url("https://example.com")
print(analysis["final_url"], analysis["http_status"])
```

The result contains `session_id`, `final_url`, `http_status`, `page_title`,
`screenshot_base64`, `visible_text`, `forms`, `orphan_inputs`, `redirects`,
`frame_navigations`, `downloads`, and `refusals`.
If the initial navigation becomes a direct download, the broker cancels it and
returns the bounded download metadata. In that download-only result,
`http_status` is `0`; page title, screenshot, and visible text are empty; and
forms and orphan inputs are empty lists.

There is no raw-browser fallback. Direct session lifecycle, `InvokeBrowser`, and
CDP stream actions are explicitly denied on worker roles. Broker unavailability
is an environment failure, while a broker HTTP 403 becomes `DestinationRefused`
with the denylist reason code.

## Trusted broker contract

The broker runs under a distinct service account and permissions boundary. Its
role permits only session lifecycle and `ConnectBrowserAutomationStream`; it
does not permit `InvokeBrowser`. Before a page exists, `browser_guard.py`:

1. Resolves and vets the initial target, failing closed.
2. Starts AgentCore and connects over CDP inside the broker pod.
3. Creates an offline context with service workers disabled.
4. Installs HTTP routing and WebSocket refusal before creating a page.
5. Re-resolves every navigation, redirect, popup, and subresource.
6. Fetches over a socket pinned to one vetted address with the original Host and
   TLS SNI, response/time/byte limits, and no browser-network fallback.
7. Closes the page, context, CDP connection, and AgentCore session before
   returning the bounded evidence response.

The broker API accepts only capture options. It never returns an AWS credential,
CDP URL/header, Playwright object, or arbitrary browser command channel.
