"""adp-trigger — CLI for agent-to-agent dispatch via POST /agent/trigger.

Issue #2153: Provides an API-based trigger path as an alternative to
@agent-<persona> comment mentions. Reads lineage context from the pod
environment (ADP_CORRELATION_ID, ADP_MESSAGE_ID, ADP_CHAIN_DEPTH) and
SigV4-signs the request with the pod's IRSA credentials.

Must run inside an agent pod — fails fast with a clear error if lineage
env vars are missing.
"""
