# Direct AgentCore Browser contract

The default `URL_ANALYSIS_BROWSER_MODE=native` connects the worker directly to
AWS-managed `aws.browser.v1` with `BrowserClient.start`, signed CDP headers and
Playwright `connect_over_cdp`. Worker IAM grants session start/get/list/stop and
`ConnectBrowserAutomationStream` in the configured region. No generic runtime,
Code Interpreter or `InvokeBrowser` permission is needed.

`browser_client.analyze_url` and `capture_url` use bounded one-shot processes.
`investigation_request(start|step|close)` preserves a Playwright process within the
worker across CLI calls using a private Unix socket. It uses the worker identity,
not an HTTP service or a separate credential-bearing broker. Session tokens refer
to that pod and cannot be resumed on a different worker. Actions are never replayed
after uncertain errors. Close is idempotent; a watchdog terminates hung driver
processes and independently attempts StopBrowserSession. AgentCore expiry remains
the backstop when an entire pod disappears.

Native Chromium handles page networking. The collector does not set offline mode,
block service workers/WebSockets/popups, rewrite popup targets, intercept requests,
or replay HTTP through pinned Python sockets. Automatic page requests can include
POSTs. Downloads are recorded as offers and cancelled; payloads are not executed.
The analyst's authorized scope and read-only investigation instructions still apply.
Host scope limits chosen actions, not page-generated requests or redirects.

AWS documents container/session isolation and automatic TTL termination:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-tool.html
This does not establish parity with the removed per-request private-address filter.
The standard AWS-managed browser is used without customer VPC connectivity or a
custom execution role. Do not claim that every private destination is filtered.

`URL_ANALYSIS_BROWSER_MODE=broker` explicitly selects the legacy guarded path for
old deployments during migration. An old broker URL alone does not select it.
Native sessions never fall back to the broker. The broker deployment is scaled to
zero only after existing leases drain and native worker acceptance succeeds.
