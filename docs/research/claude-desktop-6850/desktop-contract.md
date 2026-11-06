# Claude Desktop gateway contract research

Checked 2026-10-11 against publicly accessible Anthropic documentation. This
note concerns **Claude Desktop**, not Claude Code or the Anthropic API client.
`not_documented` means that the cited public sources do not establish a Desktop
gateway integration; it does not establish that the application cannot support
one. Do not infer Desktop configuration from Claude Code settings or API syntax.

| Contract item | Status | Public evidence and limit |
|---|---|---|
| `settings_format` | `not_documented` | [Install Claude Desktop](https://support.claude.com/en/articles/10065433-install-claude-desktop), checked 2026-10-11, does not specify a writable settings schema for an alternative Anthropic gateway. |
| `auth_scheme` | `not_documented` | [Install Claude Desktop](https://support.claude.com/en/articles/10065433-install-claude-desktop), checked 2026-10-11, does not specify bearer-token authentication for a custom gateway in Desktop. The [Messages API](https://platform.claude.com/docs/en/api/messages) documents API callers, not Desktop's provider configuration. |
| `credential_helper` | `not_documented` | [Claude Code settings](https://code.claude.com/docs/en/settings), checked 2026-10-11, document Claude Code's `apiKeyHelper` only; [Install Claude Desktop](https://support.claude.com/en/articles/10065433-install-claude-desktop) does not document an equivalent Desktop setting. |
| `models_endpoint` | `not_documented` | [List models API](https://platform.claude.com/docs/en/api/models/list), checked 2026-10-11, documents the API endpoint but not whether Desktop calls it or accepts a custom gateway for it. |
| `count_tokens_endpoint` | `not_documented` | [Count tokens API](https://platform.claude.com/docs/en/api/messages-count-tokens), checked 2026-10-11, documents an API operation, not Desktop's use of that operation with a custom gateway. |
| `messages_endpoint` | `not_documented` | [Messages API](https://platform.claude.com/docs/en/api/messages), checked 2026-10-11, documents an API operation, not Desktop's use of that operation with a custom gateway. |

**Implementation choice:** install the deployment-pinned ADP credential helper
but do not edit any Desktop configuration file. Print the proposed connection
details for a user to apply only if their Desktop build offers a compatible
provider setting. A successful ADP gateway check establishes the gateway's
response, not that Claude Desktop accepts these settings or invokes the helper.
The Desktop GUI integration remains unverified until the epic owner's manual
