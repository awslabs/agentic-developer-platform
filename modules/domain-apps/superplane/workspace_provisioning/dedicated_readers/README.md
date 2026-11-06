# Native dedicated workspace readers

`python -m workspace_provisioning.dedicated_readers` runs as a separate installed
management Deployment. It does not receive the paid worker's credentials, an
operator profile, database access, or the manager's signing key.

App Terraform in `infra/dedicated-readers` creates its IRSA role and exact EKS
entries without associating an EKS access policy. A fresh native bootstrap owns
four journalled Kubernetes grants: named reader ServiceAccount GET/TokenRequest,
named namespace GET, and namespace inventory LIST. Failure revokes these grants;
successful bootstrap retains them independently of its expired execution lease.
The reader ServiceAccount retains only observation permissions.

The scoped API checks the current original claim, component UIDs and live
execution before registration. After registration it requires canonical dedicated
workspace/cluster ownership and the positively revoked, retained bootstrap journal.
It returns expiring metadata, never credentials. Shared memberships cannot enter
this path. Existing journals without the reader grants are not rewritten.

The renewer verifies its immutable IAM role and EKS entry, pins cluster TLS, and
issues a 15-minute TokenRequest for the exact ServiceAccount. It authenticates
the returned token and checks effective permissions across every namespace,
rejecting secrets, mutation, wildcard, exec, attach and proxy access. Renewal
starts five minutes before expiry. The only local files contain public CA data
and process health; token bytes stay in memory and the owned Kubernetes Secret.

Projection uses the installed Secret UID, resourceVersion and a per-workspace
digest receipt. Token and receipt change in one CAS write. Other workspace keys
are preserved; foreign keys are refused. A lost write response is observed,
never blindly replayed. An explicitly inactive target removes only its owned
projection. A failed registry read does not invent revocation; the existing token
expires within its 15-minute bound. Already issued tokens remain valid until
expiry or ServiceAccount deletion. Revoking IRSA stops future issuance, while
native retirement removes the reader identity and journal-owned RBAC.

Installation is supported only in management mode. Prepare the empty projection
through `installation.dedicated_readers.prepare_projection` under the native
Installer lock, then pin its returned UID in `dedicated_readers.projection`.
Use the reviewed Terraform outputs for role/entry identities and cluster TLS.
Supply a separately installation-tagged Secrets Manager credential pair at
`adp/<environment>/superplane/dedicated-reader-auth`. The source contains exactly
`credential` and `signing_key`, each at least 48 characters. The installer appends
its exact workspace reader scopes after composing the existing recovery identity.
The observation source separately authorizes the selected workspace IDs for the
existing controller and monitor. Its original credentials and keys are retained.

The normal source, owner, saved-plan and admission gates still apply. Image
preflight proves the selected API image includes the reader runtime and endpoint
before rollout. API/worker/controller changes should be grouped in one qualified
maintenance pass. Installation readiness is only service health: acceptance still
requires native registration, ready nodes, fresh signed health and authenticated
workspace UI checks. A stopped post-component bootstrap needs its separately
qualified recovery boundary; restarting a pod or installing this service alone
cannot reauthorize it.
