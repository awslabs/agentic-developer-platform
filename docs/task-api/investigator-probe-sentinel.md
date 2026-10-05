# Investigator probe sentinel validation

The investigator probe accepts HTTP 200 with a provider request ID and one
completed assistant text message (`type=message`, `role=assistant`,
`stop_reason=end_turn`). Its sole text block, after trimming whitespace, must be
`OK` or `OK.`. Prose, extra blocks, tools, unknown or truncated completion states,
empty responses and transport uncertainty remain failures. No request retry is added.

This is a worker validator correction for future authorized probe runs only.
The request body, request shape hash and harness revision are unchanged. It adds
no recovery/adjudication API, changes no budget counters and does not rewrite
existing failed slots or evidence.

The original 2026-09-26 investigator attempt used slot
`716bd80f-91df-4d66-8ae0-6e9081c8e9d8` in cycle
`83fd8c93-0ec2-4fbc-a4a1-278f3e840956`, with request shape
`46e891f50d92e8e113b6017ae643bc668d748eb5dd50fea815613e78ae7b20ae`.
AWS Bedrock's original invocation log for request
`7a938503-c609-4d2f-ae6d-58e1c6d57937` records text `OK.`, `end_turn`,
13 input tokens and 5 output tokens. The previous exact `OK` comparison caused
`provider_response_unconfirmed`; its canonical outcome remains `error`.

Original receipt: CloudWatch group `/aws/bedrock/adp-dev/model-invocations`,
stream `aws/bedrock/modelinvocations`, event
`39927026401337024684454607651641701730369453622105735168`, timestamp
`2026-09-26T02:29:45Z`. The retained raw CloudWatch JSON envelope has SHA-256
`32c56535527e94b32987cd466220087e725d2fc3b2cd1bf49a74a3b749e38a95`.
This receipt explains the failed validator; it does not promote the old record.

Deployment requires the agent runtime image containing this worker probe.
No gateway, CLI, IAM or request-fingerprint changes are needed. Existing
same-cycle slot/budget and evidence-expiry guards continue to apply.
