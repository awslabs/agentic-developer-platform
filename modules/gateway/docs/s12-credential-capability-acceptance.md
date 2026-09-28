# S12 capability integration

The initial PR #6062 added raw-read/materialize registry capabilities. A09 #5950
subsequently made authenticated run/owner binding mandatory. The S12 integration
adds exact registry capabilities for listing, proxying, role assumption and task
sessions, and removes the obsolete owner-enforcement switch.

See [the complete producer inventory, finding disposition and rollout order](../../../docs/security/s12-credential-capabilities.md).
The canonical implementations are `src/internal/credential_authorization.py`,
`src/agentauth/broker_identity.py` and the existing protected-worker registry seed.
