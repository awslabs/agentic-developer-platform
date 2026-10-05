# Shared membership credential primitives

These helpers perform provider effects through explicitly supplied `KubeGrants`
transports. They do not install authority, schedule renewal or activate a workspace.
The existing workspace bootstrap and credential controller must compose them with
durable intent journals and fresh authorization.

1. After reserving membership and observing its namespace UID, build
   `CredentialBinding(membership, namespace_uid, revision, scope)`. Revision is an
   integer from 1 through 2**31-1; scopes are `reader` and `mutator`.
2. Journal and apply `delegation_specs(binding)` through the existing authority
   journal. These are a generation/revision-specific ServiceAccount, Role and
   RoleBinding. Persist the actual ServiceAccount UID **before issuance**.
3. `MemberIssuer(grants, authorize, audience=...)` uses the pinned data-plane
   transport. `issue(binding, service_account_uid=..., lifetime_seconds=900)`
   performs the real TokenRequest, checks exact delegation/UIDs and returns an
   `IssuedCredential` whose repr redacts token material. The audience must come
   from installed cluster configuration. Never log its kubeconfig or serialize
   its private token field into a journal.
4. `SecretProjector(grants, authorize, namespace=..., namespace_uid=...,
   secret_name=..., secret_uid=..., scope=...)` uses separately authorized pinned
   control-plane transport. Secrets must already exist. Reader and mutator use
   distinct Secrets and consumers. `publish(credential,
   certificate_authority_data=..., previous=receipt)` changes only that
   workspace's kubeconfig key and ownership annotation, guarded by Secret UID and
   resourceVersion. Its public receipt includes binding metadata, content digest,
   Secret UID and observed resourceVersion. Projection is not consumer acceptance.
5. Journal actual consumer acknowledgement of the current binding/revision before
   activating it. Consumers must compare the `superplane.aws-e/membership`
   kubeconfig extension against fresh authorized database state; the extension
   alone grants nothing.
6. Renewal repeats this process with a new revision and ServiceAccount under
   current installed cluster-controller authority or fresh admitted work. The
   callback `authorize(binding, action)` is invoked around provider requests;
   actions are `issue`, `revoke`, `project`, and `unproject`. It must enforce the
   appropriate current journal state, membership, cluster authority and fencing.
   An expired/completed bootstrap operation is never a renewal authority.
7. Fence the old revision, call `remove(binding, receipt)` and
   `revoke(binding, service_account_uid=...)`, then clean up its journalled RBAC.
   Never delete the cluster-owned issuer role/access entry during member cleanup.

CAS conflicts and ambiguous writes require journal reconciliation; no blind retry
overwrites a newer revision. ServiceAccount deletion uses UID and resourceVersion
preconditions and revokes only that revision. Kubelet projection and Kubernetes
authorization/token invalidation can lag; a successful Secret patch or delete
response is not proof that every consumer has stopped using the old credential.

There are no delegated cluster-scoped NodePool reads, Secret/token-mint privileges,
or namespace/RBAC writes. Existing consumers that require such rights need their
scope corrected during integration. Admission policy must also prevent workload
pods selecting privileged platform ServiceAccounts; none should reside in member
namespaces.
