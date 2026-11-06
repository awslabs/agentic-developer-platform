# Dev Box as an owner-scoped user service

**Decision:** Dev Box belongs under `modules/user-services/dev-box/` because
its workspace, access and billing have one authenticated owner. Administrative
template and policy metadata is shared only for admission; it never grants
admins access to an owner's files or a connect-as-user path.

The general user-service restriction against executing submitted code continues
to apply to services running in the gateway or shared controller. Dev Box may
execute the owner's code only inside a separately isolated owner EC2 instance
with a dedicated limited role and private network (#7369). A guest cannot use
the controller's AWS authority. Machine identity and revocation are bound to
owner, deployment and generation (#7371), including after replacement.

Unlike a synchronous database delete, release accepts a pending operation and
preserves box, device, resource and usage inventory while cleanup remains
unverified. A failed or unknown provider result is visible and retryable;
retained storage remains attributed and metered. Only reconciliation can mark
release complete after checking owned resources (#7370, #7372). The gateway
owns the single database migration and checks current membership and configured
policy before admission. Provider execution, guest enrollment, spend controls
and live qualification require their separate reviewed stories and operator
approval. No deployment or cloud spend is authorized by this decision.
