# Credential destination binding

The gateway enforces destination binding by default for stored credentials whose
service is `github`, `openai`, `anthropic`, `stripe`, or `slack`. For example, a
GitHub credential can reach `api.github.com`, but a request to send it to
`slack.com` receives HTTP 403 with `credential_host_binding_denied`. The check uses
the resolved credential's service and runs before retrieving its secret. Denials
are audited as `vault_proxy_request_denied`.

Destinations must also pass `BG_VAULT_PROXY_HOST_ALLOWLIST`, HTTPS and public-IP
validation. The binding map does not expand that allowlist. Custom services,
including deployment-specific Jira and AWS services, retain their existing global
allowlist checks and emit `service_unmapped` warnings; this change does not add
per-tenant destination bindings for them.

## Existing deployments

Both gateway deployment paths read
`/adp/{env}/gateway/vault-enforce-credential-host-binding` from SSM and render it as
`BG_VAULT_ENFORCE_CREDENTIAL_HOST_BINDING` in the gateway ConfigMap. Missing or
empty values resolve to `true`; invalid values stop deployment. An explicit
`false` is retained and produces a deployment warning.

Before enabling an existing deployment, review its `Credential egress binding
VIOLATION` warnings and credential service metadata against the reviewed map in
`modules/gateway/src/internal/credential_egress.py`. Confirm legitimate hosts are
also globally allowlisted. No credential values are needed for this review.

Set the SSM parameter to `true`, then use the normal gateway deployment path, or
apply the same one-key ConfigMap update and perform a rolling restart of the
existing gateway deployment. A ConfigMap change alone does not refresh running
containers' environment variables. Verify every replacement pod's effective
setting, gateway health, an allowed provider destination and a rejected mismatch.
Use synthetic credentials or an isolated guard check for verification; never
send a real credential to a mismatched destination as a test.

For a temporary rollback, explicitly set the parameter and running gateway
configuration to `false`, restart the gateway, and record why shadow mode is
needed. Restore `true` after resolving the compatibility issue.

This control is independent of MFA, Cognito threat protection and chat-model
policy. Changing it does not change those settings.
