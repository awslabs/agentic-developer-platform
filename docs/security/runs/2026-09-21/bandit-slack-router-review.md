# Slack response router transport boundary

Original selector `bandit|bandit-results.sarif|run=0|ri=653`, MEDIUM B310,
`modules/agent-factory/gateway/lambdas/response/routers/slack.py` line 55,
frozen source `b1d0894c17c686f27c2747057dead0b5a0e6b17e`, remains owned by #6108.

The initial Slack API URL is a fixed source-controlled HTTPS endpoint; request
metadata cannot select it. This is not an arbitrary initial URL/SSRF finding.
However, urllib's default redirect handling forwards the Bearer bot token to a
redirected origin. Responses from the authenticated endpoint should not grant
that authority to a second endpoint.

The bounded fix uses a per-request opener refusing redirects, adds a ten-second
timeout, and avoids logging raw exceptions, secret-service errors or API error
payloads. No global opener is installed. Successful channel/thread/message
payloads, boolean success/failure behavior and the five-minute token cache are
preserved.

`modules/agent-factory/tests/lambda/test_slack_router_transport.py` uses local
HTTP servers and a synthetic token only. Five redirect codes are checked:
301/302/303 reproduce token forwarding with the original code; 307/308 are
already refused by urllib for POST. With the fix no redirected destination
receives the token. Further tests cover successful payloads/cache, missing
channel, timeout, API failures and secret/transport error log redaction.
All 10 focused tests and all 473 Lambda tests on this source base pass.
Changed Python lint/format and diff checks also pass.

The source observation is `fixed-source-runtime-open`: original identity and
MEDIUM severity remain, and the other original selectors are not reclassified.
No real Slack endpoint, token or channel was used. No response Lambda deployment,
gateway/tick write, cluster mutation or live acceptance was performed. An actual
rollout and acceptance check remain required before declaring this deployed.
