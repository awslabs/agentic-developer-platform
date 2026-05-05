"""adp-cred CLI — vault credential access for hosted agent pods.

Subcommands:
  adp-cred list                           List available credentials (metadata only)
  adp-cred http METHOD URL --service SVC  Proxy HTTP request with injected credential
  adp-cred materialize --service SVC      Materialize file-type credential (prints URL)
  adp-cred raw --service SVC              Return raw credential value to stdout

Environment variables (set by entrypoint.py from envelope):
  ADP_USER_ID         — user whose credentials to access
  ADP_AGENT_ID        — agent persona making the request
  ADP_TASK_ID         — current task identifier
  VAULT_GATEWAY_URL   — base URL of the gateway internal API
  VAULT_INTERNAL_API_KEY — shared secret for internal auth
  ENABLE_USER_CREDENTIALS — must be "1" or "true" to allow operations

Issue #137: Vault Phase 4
"""

__version__ = "0.1.0"
