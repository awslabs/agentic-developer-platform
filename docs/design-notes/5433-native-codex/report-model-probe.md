# GitHub report persona model qualification

Architect, Product, PM and Intent Refinement now have separate SDK-generated model request contracts. The qualification capture runs the actual shared session and GitHub repository tool definitions with the exact gateway catalogue snapshot. It stops at the provider callback: no successful model response or tool execution enters the capture.

The bounded provider check reuses the dedicated probe identity, durable claim/start/complete protocol, destination credentials and evidence admission. It sends one nonstreaming request, capped at 512 output tokens, and requires a confirmed provider request ID and an OK response. It does not prove report quality or publication; those require a live GitHub invocation.

Only synthetic environment paths, date and UTC spelling are normalized. Persona instructions, shared rules, effort and tool schemas remain in the digest. CI captures each of four personas on three models twice and compares the manifests and gateway catalogue. Installed workers repeat the capture before obtaining provider credentials.

These profiles use the existing SDK probe selector (`ADP_NATIVE_PROBE_PERSONA`) and remain outside the Task persona registry. Report capture runs from `/app/codex-harness/scripts/report-probe-cli.mjs`; developer and reviewer retain their existing native capture.

Live pre-credential qualification also exposed a stale display-name check in the probe route. The route now binds the real IAM adapter's canonical `iam-agent:persona-model-probe` identity, immutable registry ID, IAM authentication source, platform organization and internal scope. The acceptance test uses the real registry adapter, including a renamed display name; tenant lookalikes remain rejected.
