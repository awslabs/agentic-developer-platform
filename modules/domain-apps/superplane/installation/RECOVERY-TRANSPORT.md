# Private recovery transport

Superplane can install a private HTTPS recovery listener with its API. It is
optional: omitting `recovery_transport` creates no listener, Service, policy or
secret dependency. Ordinary ADP setup does not invoke this installer.

Select these two installation-owned Secrets Manager names in the private
environment file (replace `dev` with the selected environment):

```yaml
recovery_transport:
  tls_secret: adp/dev/superplane/recovery-tls
  credential_secret: adp/dev/superplane/recovery-credential
```

Prepare the pair through the selected bounded installer connection:

```bash
./modules/domain-apps/superplane/deploy.sh --prepare-recovery-transport \
  --environment /private/environment.yaml --release-lock /private/release-lock.yaml \
  --output /private/recovery-preparation
# Inspect the emitted plan, then execute its exact digest:
./modules/domain-apps/superplane/deploy.sh --prepare-recovery-transport \
  --environment /private/environment.yaml --release-lock /private/release-lock.yaml \
  --output /private/recovery-preparation --resume --execute \
  --approved-plan-sha256 '<emitted plan digest>'
```

The installer owner must first provide reviewed, exact-name read/create/tag
permissions for these two secrets through its existing Terraform policy owner.
Creation requires the `SuperplaneInstallation` tag. This command does not add
IAM permissions. Existing secrets must already belong to the selected
installation; it never adopts or rotates credentials. A partial first execution
reuses the saved TLS secret to finish the matching credential. Lost installation
locks use `--resume --recover-lock --confirm-stopped <run-id>` after confirming
the original installer stopped. Receipts contain references and versions only.

The server certificate lasts 90 days and names exactly
`superplane-recovery.<namespace>.svc.cluster.local`. Preparation and installation
refuse certificates with seven or fewer days remaining. Renewal requires a
separate reviewed rotation of both owned secrets and a normal installer rollout;
automatic certificate renewal is not provided by this version. The CA signing
key is discarded after issuance. The Gateway secret contains only
`authorization` and `ca_pem`, never the server private key or observation signing
key. Its observer has only `controller_recovery/<selected-org>` authority.

Normal Superplane installation validates the pair, projects the TLS certificate
and key to the API's TLS sidecar, and adds the observer to the existing API
submitter projection. It preserves the original monitor/controller secret.
Use an API image built with `app.recovery_transport` and a Gateway image with
`domain_observation_transport`; older images do not implement this contract.

The binding's `observation_url` is
`https://superplane-recovery.<namespace>.svc.cluster.local:8443` and
`observation_credential_secret_id` is the actual ARN of the prepared Gateway
credential secret. The shared Terraform binding owner grants only its existing
read of that reference. The Gateway creates a TLS context for that binding,
verifies the hostname and chain, and retains normal TLS verification for
existing raw-credential bindings. Global trust settings are unchanged.

Private bindings load only their selected CA into a client TLS context and
require certificate and hostname verification. This preserves compatibility with
previous installer certificates, which lack key-identifier extensions, across
Python 3.13's stricter default-context behavior. New certificate generation adds
subject/authority key identifiers and server key usage and is tested with strict
X.509 verification. Existing certificates and operation-bound CA values are not
silently rotated.

The Service is ClusterIP-only. Network policy admits port 8443 only from the
Gateway pods in the selected namespace and the installation-owned native worker. The sidecar has only TLS mounts and is
excluded from IRSA injection. It forwards six fixed recovery actions and the operation-scoped bootstrap
observation GET to its colocated API over loopback, bounds request/response
bodies, and refuses redirects and other paths. The bootstrap route retains its
separate operation-bound read-token authentication. The API still enforces credential scope, original admission and
current recovery claim. Management-only mode continues refusing recovery until
runtime activation. This transport does not establish runtime readiness,
approve a workspace, or replace the authenticated saved-runtime-plan approval.

Verify the listener from a permitted Gateway pod with the binding's CA, including
wrong-hostname/untrusted-certificate rejection, denied unauthenticated requests,
and a non-admitted claim that cannot cause recovery. Verify that public Gateway
routes still deny internal recovery paths. A successful credential-preparation
receipt alone does not claim those live checks passed.

For native bootstrap, set the reviewed lifecycle runtime's `management_api_origin`
to the same private HTTPS origin, and `management_ca_pem` to the prepared public
CA certificate. Both values enter the lifecycle policy digest and therefore the
operation approval. Managed lifecycle foundations refuse another origin or CA
during credential preflight. Native public-HTTPS egress adds only the exact
owned API peer on TCP8443, plus the independently verified Auto Mode DNS resolver
when applicable. No private key or bearer credential belongs in the policy.

Both API and worker images must include this bootstrap trust support. An older
worker's policy parser correctly rejects the new optional trust field. Preparing
the two secrets can precede those builds; it does not install an image or assert
that the endpoint is already reachable.
