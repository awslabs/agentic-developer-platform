"""adp-cred CLI — vault credential access for hosted agent pods.

Subcommands:
  adp-cred list                           List available credentials (metadata only)
  adp-cred http METHOD URL --service SVC  Proxy HTTP request with injected credential
  adp-cred materialize --service SVC      Materialize file-type credential (prints URL)
  adp-cred raw --service SVC              Return raw credential value to stdout

Environment variables (set by entrypoint.py from envelope):
  ADP_USER_ID             — user whose credentials to access
  ADP_AGENT_ID            — agent persona making the request
  ADP_TASK_ID             — current task identifier
  ENABLE_USER_CREDENTIALS — must be "1" or "true" to allow operations

Auth mode (one of the following):
  ADP_GATEWAY_ENDPOINT    — API Gateway invoke URL; uses IRSA/SigV4 (preferred)
  VAULT_GATEWAY_URL +
  VAULT_INTERNAL_API_KEY  — direct in-cluster URL + shared secret (legacy)

Issue #137: Vault Phase 4
Issue #575: IRSA/SigV4 migration (dual-auth)
"""

__version__ = "0.1.0"
