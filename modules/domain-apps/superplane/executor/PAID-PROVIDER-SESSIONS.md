# Native lifecycle AWS sessions

Native lifecycle workers use only their Gateway invocation IAM identity. The
paid `provider-preflight` and `provider-session` routes re-read the protected
run/pod, original admission, current human membership and approval, live lease,
sealed credential reference and target, verified connection version, and current
workspace delegation. The shared identity owner must supply
`current_human_identity`; absence is a dependency failure, never a raw-subject
fallback. Its membership ID selects the tenant-local canonical user for role
ownership and session tags.

Provider sessions live in memory for at most 900 seconds and never outlive the
paid grant, approval or runtime deadline. A preflight can succeed with less time
remaining because it issues no AWS session. Minting/renewal refuses when fewer
than 900 seconds remain. A 900-second phase budget is therefore unusable after
dispatch overhead: the reviewed native policy and approved phase envelope must
provide a larger duration. A 3600-second phase is appropriate for a demo expecting
an EKS create to take roughly 20 minutes; the installer does not increase an
approved budget automatically.

Every refreshed session is checked using AWS GetCallerIdentity against the
broker's account, assumed-role ARN and immutable role ID. Scoped cleanup must
keep the same provider role ID. Actor role chaining retains its existing fresh
operation checks and cannot extend the authoritative runtime deadline.

The existing Terraform/AWS CLI subprocess runs with a private, random-token
loopback AWS container-credential endpoint scoped to that process lifetime.
SDK refresh requests revalidate operation authority and obtain refreshed session
credentials. Child environments contain an endpoint and bearer token, not AWS
keys. No raw provider credentials are persisted. The endpoint closes when the
process finishes or authority fails.

Bootstrap EKS cleanup uses a server-constructed policy limited to four actions
on one immutable access-entry ARN. The current domain journal and reservation
must attest the exact operation, claim, generation and revoke intent. Current
EKS entry identity must still match before release. A separately admitted
retirement operation needs its own generation-owned cleanup attestation; an old
bootstrap journal does not authorize a different operation.

This change covers native dedicated lifecycle execution. Recovery, new-account
creation, shared-cluster bootstrap and controller workload credentials are not
implicitly promoted to this broker contract. Source tests do not constitute
live STS, Terraform or workspace readiness evidence.
