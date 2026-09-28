# AgentCore Browser API Contract Extract

Focused extract from the AgentCore Developer Guide for URL analysis orchestration.
This is the only reference you need for writing orchestration scripts.

## Service Client

```python
import boto3
client = boto3.client('bedrock-agentcore', region_name='us-east-1')
```

## Session Lifecycle

### Start a session

```python
response = client.start_browser_session(
    browserIdentifier="aws.browser.v1",
    name="url-analysis-session",
    sessionTimeoutSeconds=300,  # max 1800, default 900
    viewPort={
        'height': 819,
        'width': 1456
    }
)

# Response:
# {
#     "sessionId": "abc123...",
#     "liveViewUrl": "https://..."  (may not be present in all regions)
# }
session_id = response["sessionId"]
```

### Get session info (includes WebSocket endpoints)

```python
response = client.get_browser_session(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id
)

# Response:
# {
#     "browserIdentifier": "aws.browser.v1",
#     "sessionId": "...",
#     "status": "READY" | "TERMINATED",
#     "createdAt": "2025-07-14T22:16:40.713Z",
#     "lastUpdatedAt": "...",
#     "sessionTimeoutSeconds": 300,
#     "streams": {
#         "automationStream": {
#             "streamEndpoint": "wss://bedrock-agentcore.<region>.amazonaws.com/browser-streams/aws.browser.v1/sessions/<session-id>/automation",
#             "streamStatus": "ENABLED"
#         },
#         "liveViewStream": {
#             "streamEndpoint": "https://bedrock-agentcore.<region>.amazonaws.com/browser-streams/aws.browser.v1/sessions/<session-id>/live-view"
#         }
#     },
#     "viewPort": {"height": 819, "width": 1456}
# }
```

The `automationStream.streamEndpoint` is the CDP WebSocket URL for Playwright.

### List sessions

```python
response = client.list_browser_sessions(
    browserIdentifier="aws.browser.v1",
    # Optional:
    status="READY",  # filter by status
    maxResults=10
)
# Response: {"items": [{"sessionId": "...", "name": "...", "status": "..."}]}
```

### Stop a session

```python
response = client.stop_browser_session(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id
)
# Returns empty dict on success
# ResourceNotFoundException if already stopped (safe to ignore)
```

## Browser Interaction: Two Approaches

### Approach 1: Playwright via CDP (PREFERRED for URL analysis)

Connect Playwright to the session's CDP WebSocket. This gives you full browser
automation: navigation, DOM access, network interception, screenshots.

```python
from playwright.sync_api import sync_playwright
from bedrock_agentcore.tools.browser_client import BrowserClient

# Start session
bc = BrowserClient(region="us-east-1")
bc.start()

try:
    # Get CDP WebSocket URL + auth headers
    ws_url, headers = bc.generate_ws_headers()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url, headers=headers)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        # Navigate
        page.goto("https://example.com", wait_until="networkidle")

        # Screenshot
        screenshot_bytes = page.screenshot(full_page=True)

        # Extract text
        text = page.inner_text("body")

        # Get page title
        title = page.title()

        # Detect forms
        forms = page.evaluate("""
            Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action,
                method: f.method,
                fields: Array.from(f.querySelectorAll('input')).map(i => ({
                    name: i.name, type: i.type, hidden: i.type === 'hidden'
                }))
            }))
        """)

        page.close()
        browser.close()
finally:
    bc.stop()
```

**If `bedrock_agentcore` package is not available**, you can construct the WebSocket
URL manually from `get_browser_session` response:

```python
session_info = client.get_browser_session(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id
)
ws_url = session_info["streams"]["automationStream"]["streamEndpoint"]
# Connect with SigV4-signed headers (use botocore's auth)
```

### Approach 2: InvokeBrowser OS Actions

For OS-level interactions (dialogs, CAPTCHAs, keyboard shortcuts, full-desktop
screenshots). This is a REST API - no WebSocket needed.

```python
response = client.invoke_browser(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id,
    action={
        "<actionType>": { ... }
    }
)
# Response: {"result": {"<actionType>": {"status": "SUCCESS", "error": null}}}
```

**Supported action types:**

#### Mouse Actions

All coordinates must satisfy: `1 < x < viewportWidth-2`, `1 < y < viewportHeight-2`.
Default viewport: 1456x819.

| Action | Required | Optional | Description |
|--------|----------|----------|-------------|
| `mouseClick` | `x`, `y` | `button` (LEFT/RIGHT/MIDDLE), `clickCount` (1-10) | Click at coordinates |
| `mouseMove` | `x`, `y` | - | Move cursor |
| `mouseDrag` | `startX`, `startY`, `endX`, `endY` | `button` | Drag operation |
| `mouseScroll` | `x`, `y` | `deltaX`, `deltaY` (-1000 to 1000) | Scroll. Negative deltaY = scroll down |

```python
# Click example
response = client.invoke_browser(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id,
    action={"mouseClick": {"x": 728, "y": 50, "button": "LEFT", "clickCount": 1}}
)
```

#### Keyboard Actions

| Action | Required | Optional | Description |
|--------|----------|----------|-------------|
| `keyType` | `text` (max 10000 chars) | - | Type a string (ASCII only) |
| `keyPress` | `key` | `presses` (1-100) | Press a named key N times |
| `keyShortcut` | `keys` (list, max 5) | - | Key combination (e.g. ["ctrl", "a"]) |

Supported key names (lowercase): `a-z`, `0-9`, `enter`, `tab`, `space`, `backspace`,
`delete`, `escape`, `ctrl`, `alt`, `shift`, `up`, `down`, `left`, `right`.

```python
# Type URL into address bar
response = client.invoke_browser(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id,
    action={"keyType": {"text": "https://example.com"}}
)
# Press Enter
response = client.invoke_browser(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id,
    action={"keyPress": {"key": "enter"}}
)
```

#### Screenshot Action

| Action | Required | Optional | Description |
|--------|----------|----------|-------------|
| `screenshot` | - | `format` (PNG only) | Capture full OS desktop |

```python
import base64

response = client.invoke_browser(
    browserIdentifier="aws.browser.v1",
    sessionId=session_id,
    action={"screenshot": {"format": "PNG"}}
)
if response['result']['screenshot']['status'] == 'SUCCESS':
    image_bytes = base64.b64decode(response['result']['screenshot']['data'])
```

**IMPORTANT:** The screenshot captures the full OS desktop, not just the browser
viewport. Format must be uppercase `"PNG"`.

## Gotchas and Known Issues

1. **No `navigate` action in InvokeBrowser.** InvokeBrowser is OS-level only.
   To navigate, either use Playwright via CDP (preferred) or click the address
   bar + type URL + press Enter via InvokeBrowser actions.

2. **No `evaluate` action in InvokeBrowser.** JavaScript execution requires CDP.
   Use Playwright's `page.evaluate()` via the WebSocket connection.

3. **No `getHar` action.** HAR capture requires Playwright's network interception
   or CDP's Network domain. Not available via InvokeBrowser.

4. **`browserIdentifier` is required** on ALL session API calls (start, get, stop,
   list, invoke). Always use `"aws.browser.v1"`.

5. **Screenshot format must be uppercase `"PNG"`.** Lowercase will fail silently
   or return an error depending on the API version.

6. **Session auto-terminates** after `sessionTimeoutSeconds`. Always stop explicitly
   in a finally block anyway.

7. **WebSocket URL requires SigV4 auth headers.** The `BrowserClient.generate_ws_headers()`
   method handles this. If constructing manually, use botocore's SigV4 signer.

8. **Coordinate bounds are strict.** For a 1456x819 viewport: valid x is 2-1453,
   valid y is 2-816. Out-of-bounds returns ValidationException (HTTP 400).

9. **`keyType` is ASCII only.** Non-ASCII characters are silently skipped.

10. **Session status values:** `READY` (active, can interact), `TERMINATED` (stopped).

## Error Handling

| Exception | HTTP | Meaning |
|-----------|------|---------|
| ValidationException | 400 | Bad input (coordinates out of bounds, invalid params) |
| AccessDeniedException | 403 | Missing IAM permission |
| ResourceNotFoundException | 404 | Invalid browserIdentifier or sessionId |
| ServiceQuotaExceededException | 402 | Quota limit reached |
| ThrottlingException | 429 | Rate limited - back off and retry |
| InternalServerException | 500 | AWS-side failure - retry with backoff |

Retry strategy: exponential backoff for 429 and 500, max 3 attempts.

## IAM Permissions Required

```json
{
    "Effect": "Allow",
    "Action": [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:ListBrowserSessions",
        "bedrock-agentcore:InvokeBrowser"
    ],
    "Resource": "*"
}
```

## Quotas

- Default session timeout: 900 seconds (15 minutes)
- Maximum session timeout: 1800 seconds (30 minutes)
- Default viewport: 1456x819 pixels
- keyType max length: 10,000 characters
- keyShortcut max keys: 5
- clickCount max: 10
- scroll delta range: -1000 to 1000
- Rate limits: apply per-account (ThrottlingException on exceed)
