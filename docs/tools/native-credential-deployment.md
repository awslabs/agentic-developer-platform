# Deploy native credential approval

The native issuer requires the private consumer, installed CLI, browser approval
page and issuer routes from the same reviewed release. MCP additionally requires
the local human-owned coordinator. Source tests do not establish live access.

In the Gateway Terraform owner inputs, `native_credentials_enabled = true`
provisions a dedicated RSA 3072 KMS signing key and public runtime configuration
at `/adp/<environment>/gateway/native-credentials-config`. The default is false.
Use the normal saved-plan review and deployment procedure for the confirmed
account. Review the Cognito browser client's additional
`aws.cognito.signin.user.admin` scope together with the signer and configuration.
That scope permits GetUser to recheck the browser's own session; it does not grant
administrative IAM APIs.

Only the selected Gateway service role may sign with the key, and only
RSASSA_PSS_SHA_256 is allowed even if another IAM policy grants signing access.
The configuration contains the actual immutable key ARN and the existing public
frontend origin for issuer and browser approval. It contains no credentials.
There is no provisioned user, credential, entitlement, policy approval or consent.

When the issuer configuration is enabled and valid, the administrative tool
catalogue includes `credentials.assume_role` and `credentials.materialize`, even
without the optional AgentCore broker target. These are permission entries for
the native private channel; generic facade execution remains refused. An
administrator must grant the exact tenant/team entitlement and per-user allowed
policy, and the owner must consent to both the tool and the selected credential.
Native delivery still requires a separate browser approval for each request.
Catalogue registration grants none of these permissions. Disabled deployments
do not add native entries; an enabled but incomplete configuration is unavailable.

The normal Gateway workflow and `deploy-all.sh` read and validate the SSM record
before rendering the ConfigMap. An absent record preserves disabled behavior.
An unreadable, partial, duplicate-key, malformed or wrong-account enabled record
fails deployment. Deployment errors do not echo the configuration value.
After rollout, public Cognito discovery advertises native approval only when the
issuer configuration and distinct native/browser client IDs are present. The
browser requests the GetUser scope only when discovery matches its client and
pool. Missing or older discovery preserves ordinary login. Existing sessions
without the scope must sign in again before native approval.

Verify the exact deployed image/configuration and signing key policy, then run
the real native login, separate browser approval, private child consumption and
cleanup sequence. Repeat through fresh Claude Code and Codex MCP sessions with
human-registered local profiles. Preserve original request and receipt IDs across
denial, expiry, cancellation and lost replies. Do not place human refresh tokens
or credential bytes in CI, model transcripts or test artifacts. Offline mocks
and discovery alone do not satisfy this verification.

The asymmetric key has `prevent_destroy` and a 30-day deletion window. After
provisioning, setting `native_credentials_enabled = false` attempts key removal
and is intentionally refused by Terraform. Emergency runtime disablement must
preserve the key and be recorded in the owning configuration change; do not
remove the lifecycle guard to disable a feature. Key replacement needs a reviewed
cutover because native consumers pin the immutable key and public key.

The approved provisioning flag is retained in Gateway release configuration and
by both upgrade paths. The Gateway owner workflow uses the same input reader for
its saved plan and convergence check. A first targeted enablement may omit root
outputs; an existing RSA 3072 signing key at the exact native owner address still
preserves enablement, including when the older output still says false. An
unexpected key shape stops preparation for owner review. Fresh accounts remain
disabled by default.
