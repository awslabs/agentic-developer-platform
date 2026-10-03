# Chat worker model-access boundary

Chat uses service account `adp-agent` and the dedicated
`adp-<env>-chat-worker-role`. The role retains the existing non-model runtime
policies and explicitly denies `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream`. Model requests must use the ADP gateway.
This change does not introduce per-user DynamoDB or S3 isolation.

The Python agent-gateway consumer still calls Bedrock directly. It uses service
account `adp-gateway-worker` and the existing `adp-<env>-gateway-agent-role`.
That role's web-identity trust no longer accepts the chat service account.
The main gateway service role and its model permissions are unchanged.
KEDA is allowed to assume both worker roles for queue polling.

## Applying to an existing installation

Apply the factory Terraform changes before rolling out both worker ScaledJobs,
as the full deployment script does. A chat-image-only rollout does not install
this IAM change. The existing chat service-account name is preserved so gateway
workload verification does not need a coordinated configuration change.

Changing a service-account annotation does not replace credentials or environment
variables in running pods. Stop admission of new chat work and drain existing
chat and Python worker jobs before this identity migration; old Python jobs cannot
renew web-identity credentials after the trust change. After applying and rolling
out, verify newly created chat pods receive the chat role and Python pods receive
the gateway-agent role before restoring admission. Existing STS sessions remain
valid until expiration; an immediate revocation requires a separately coordinated
session-revocation procedure. Do not report completed revocation from a Terraform
apply alone.

Acceptance in the target environment must verify chat requests still succeed
through the gateway, direct Bedrock invocation using the chat role is denied,
Python worker inference is preserved and both queues still scale. No live apply
or model invocation is performed by the repository regression checks.
