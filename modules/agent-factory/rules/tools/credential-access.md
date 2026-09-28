## Credential access

User-connected AWS accounts, GitHub tokens, and other secrets live in the vault. Never hardcode, never echo, never log credentials.

- **Verify the target**: before AWS work, use `adp-cred assume --service aws --label LABEL --exec aws sts get-caller-identity` and check
  both account and the exact assumed IAM role against the admitted assignment. A matching account with the wrong role is a refusal. Auto-injected credentials
  are not evidence that the requested connection was selected.
- **Use the selected connection**: `adp-cred assume --service aws --label <label> --exec <cmd>`.
  Verify the assumed identity through the same path before target operations.
- **Missing or expired shell credentials** do not establish that the user has no
  connected account. Inspect the broker result and connection selection first.
- **Discover**: `adp-cred list` — shows available credentials (labels + services)
- **Use a stored API key**: `adp-cred raw --service <svc> --label <label>` — prints the key on stdout for env-var injection. Pipe directly; never echo.

If the broker confirms that no authorized AWS connection is available, block the
dependent AWS action and explain the required connection at `/settings/credentials`.
Continue independent authorized work. Do not search for substitute credentials.

### Long-running work and credential refresh

Temporary sessions can expire during a long run. Distinguish the identity used to
authenticate dispatch/broker transport from the user-selected role used for target
AWS actions. Refresh through the documented mechanism for each identity; do not
replace the selected target role with the pod's workload role.

- For target credentials, obtain a fresh session through the selected `adp-cred`
  connection and verify account and role again. Keep transport refresh separate.
- Use workload web identity or Pod Identity only when actually configured for the
  transport and permitted by the environment's runbook. Do not guess role ARNs,
  assume a token mount is usable, or generalize an old successful environment.
- On expiry, attempt supported recovery and continue when verified. Record the
  actual response if recovery is unavailable; routing denial, missing connection,
  expired session and invalid web identity require different remedies.
- Reconcile any action whose response was lost before repeating it. If recovery
  cannot succeed within authorized limits, record a scoped block and advance any
  independent work; never label the assignment complete because refresh failed.


Use `adp-cred assume --service aws --label LABEL --exec COMMAND ARGS...` for each target operation. Do not fall back to ambient credentials, guess role ARNs or create a second credential service. Loading these instructions grants no access.
