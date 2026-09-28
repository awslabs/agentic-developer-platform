"""adp-trigger — CLI for agent-to-agent dispatch, monitoring and control.

Issue #2153: Provides an API-based trigger path as an alternative to
@agent-<persona> comment mentions. Reads lineage context from the pod
environment (ADP_CORRELATION_ID, ADP_MESSAGE_ID, ADP_CHAIN_DEPTH) and
SigV4-signs the request with the pod's IRSA credentials.

Issue #5028: adds ``status`` and ``control`` subcommands for delegated
monitoring and controls. The dispatch form is untouched; the new subcommands
authenticate the individual run through ADP_RUN_CREDENTIAL_FILE (reread for each
request) or ADP_RUN_CREDENTIAL rather than relying on
the shared pod IAM role, because SigV4 alone authenticates the role every worker
on the platform assumes.

Must run inside an agent pod — fails fast with a clear error if lineage
env vars are missing.
"""
