# Superplane API: verified database TLS

This describes the certificate-authority (CA) bundle every Superplane database
connection now requires, how to provision it per environment, and what to do when
a connection fails verification.

It exists because issue #5676 (A22) made certificate and hostname verification
mandatory and non-optional on every path that opens a Superplane database
connection: the API Deployment, the migration Job and the seed Job.

**Nothing here is authorized by the code change that referenced this file.**
Provisioning a secret and rolling a Deployment are live operations on a named
environment: confirm the target account and obtain that environment's normal
change authorization first, per
[the deployment guide](../adp-platform-deployment/deploy-with-agent.md).
Issue #5676 deliberately did **not** deploy, apply infrastructure, or create any
secret in any environment.

## Read this before you roll the change

**A pod with no CA bundle will not start.** That is the intended behaviour — it is
the "fail closed" half of the fix — but it means the order matters: provision the
bundle **first**, then roll the workloads. An environment that has never had a
`ca-pem` key will otherwise take a full control-plane outage for every tenant on
it until the bundle is supplied.

The migration and seed Jobs are applied **separately** from the Deployment, so
they are the most likely place a missing reference surfaces first. Apply them in a
non-production environment before production.

## What was actually wrong, stated precisely

The imprecise version of this finding is "Superplane connected to its database
without TLS". That is **false**, and believing it leads to the wrong fix.

Verification against asyncpg 0.31.0 (not inferred from parameter names):

| | Old default (no CA set) | Now |
|---|---|---|
| Resolved mode | `sslmode=prefer` | pinned verifying context |
| Encryption attempted | yes | yes |
| Certificate chain checked | **no** (`CERT_NONE`) | yes (`CERT_REQUIRED`) |
| Hostname checked | **no** | yes |
| Silent plaintext retry | **yes** | no |

So the old behaviour was *opportunistic encryption, unauthenticated, with a
silent plaintext fallback*. Two consequences worth being clear about:

* A **passive eavesdropper** on the internal network was sometimes defeated and
  sometimes not, depending on whether the encrypted attempt succeeded. Nothing
  logged which outcome occurred on any given connection.
* An **active** attacker — anyone able to answer on the database's address, via a
  service-mesh misconfiguration or a compromised workload — was not impeded at
  all. Encryption without authentication does not establish *who* you encrypted
  to.

What crosses that connection is tenant workspace records, spend and cost rows,
organisation memberships, the audit trail, and hashed tenant API keys.

## What each artefact needs

All three read the **same** secret key, `ca-pem` of the `superplane-api-db`
Secret, so one rotation covers every consumer. They consume it differently
because they are different database clients:

| Artefact | Client | Mechanism |
|---|---|---|
| `deployment.yaml` | asyncpg | `SUPERPLANE_DATABASE_CA` env var, `optional: false` |
| `db-migrate-job.yaml` | asyncpg | `SUPERPLANE_DATABASE_CA` env var, `optional: false` |
| `db-seed-job.yaml` | libpq (`psql`) | bundle **mounted as a file**, plus `PGSSLMODE=verify-full` and `PGSSLROOTCERT` |

The seed Job differs because libpq cannot accept a CA from an environment
variable at all — it requires a file path and a mode. `verify-full` is the only
libpq mode that checks **both** the certificate chain and the hostname;
`require` encrypts while verifying neither, which is the posture this change
removes.

The installer path (`installation/manifests.py`) already supplied this and is
unchanged. This runbook covers the standalone `src/superplane-api/deploy/` path.

## Provision the bundle

The value is the trusted PEM bundle for **that environment's** database endpoint.
For Amazon RDS and Aurora, that is the regional CA bundle for the endpoint's
region — not a certificate you generate.

Run against the environment's cluster, having confirmed the target account:

```bash
# 1. Fetch the regional RDS CA bundle (adjust the region to the DB endpoint's).
curl -sSf -o /tmp/rds-ca.pem \
  "https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem"

# 2. Add it to the EXISTING database Secret without disturbing DATABASE_URL.
kubectl create secret generic superplane-api-db \
  --namespace superplane \
  --from-file=ca-pem=/tmp/rds-ca.pem \
  --dry-run=client -o json \
  | kubectl patch secret superplane-api-db \
      --namespace superplane --type=merge --patch-file=/dev/stdin

# 3. Confirm the key exists before rolling anything.
kubectl get secret superplane-api-db -n superplane \
  -o jsonpath='{.data.ca-pem}' | head -c 40
```

Step 2 is a merge patch precisely so it does not overwrite the connection string
already in that Secret.

## Verify after rolling

Three checks, in order. The first two confirm the fix works; the third confirms
it fails closed, which is the part that is easy to leave untested.

```bash
# 1. The service started and reports a verified connection.
kubectl logs -n superplane deployment/superplane-api --tail=50

# 2. Ask the DATABASE whether the service's session is encrypted. This is the
#    authoritative check -- the client's own opinion is not evidence.
#    Expect ssl = t for the runtime role.
psql "$ADMIN_URL" -c \
  "SELECT usename, ssl, client_addr FROM pg_stat_ssl
     JOIN pg_stat_activity USING (pid)
    WHERE usename LIKE 'superplane_%';"

# 3. Confirm it FAILS CLOSED. In a non-production environment only, remove the
#    key and restart: the pod must refuse to start and name the setting.
#    Restore the key afterwards.
kubectl patch secret superplane-api-db -n superplane \
  --type=json -p='[{"op":"remove","path":"/data/ca-pem"}]'
kubectl rollout restart deployment/superplane-api -n superplane
kubectl get pods -n superplane -w   # expect CreateContainerConfigError
```

Check 3 is worth actually performing once per environment. A verifying client
that silently degrades looks identical to a working one until the day it matters.

## When verification fails

Symptom: pods start but connections fail with a certificate error, or the API
logs `certificate verify failed` / `hostname mismatch`.

Most likely causes, in the order worth checking:

1. **Wrong region's bundle.** RDS CA bundles are regional. A bundle from another
   region will not chain.
2. **Hostname mismatch after a failover or endpoint change.** The name in
   `DATABASE_URL` must be the endpoint the certificate is issued for. Connecting
   by an IP address, or by a CNAME the certificate does not cover, fails hostname
   verification even when the chain is fine. This is the failure mode that tends
   to appear during a failover rather than at deploy time — which means it
   surfaces at the worst moment and looks like a database availability incident.
3. **Expired or rotated server certificate.** Re-fetch the current bundle.

## The emergency override, and its limits

There is exactly one: `SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS=true`.

It is **intended for a local throwaway database only** — specifically
`src/superplane-api/deploy/integration-test.yaml`, whose in-cluster Postgres
serves no certificate at all, so there is nothing to verify. A test asserts it
never appears in a deployable manifest.

Properties you can rely on:

* It is matched against the **exact** literal `true`. `"false"`, `"0"`, `"no"`
  and `"True"` do **not** enable it, so a careless value cannot silently disable
  verification.
* It cannot be reached by omitting configuration. An absent CA is a refusal to
  start, never a downgrade.
* It logs at **WARNING** on every process start while active.
* Present trust material **wins over it**, so a stale override left in an
  environment that has since been given a CA does not keep that environment
  unverified.

**If you use it on a shared environment to restore service, treat it as an open
incident, not a configuration choice.** Record an owner and a removal date. While
it is set, every tenant record, spend row and audit entry on that environment
crosses the network without verified encryption, and the finding this runbook
exists for is live again. Prefer fixing the bundle.

## What this change does not do

* It does not rotate any credential. That is
  [the credential cutover runbook](superplane-jwt-and-db-credential-rotation.md).
* It does not restrict **which workloads** may reach the Superplane service. That
  is a NetworkPolicy, owned by the installer manifests and dependent on the
  still-open platform prerequisite #4999. Rendering a NetworkPolicy is not
  evidence that network isolation is enforced.
* It does not encrypt anything at rest.

### Local exception target validation

The exception validates the effective SQLAlchemy/asyncpg host, including any
query-string override and every host in a failover list. Only explicit loopback
addresses, `localhost`, absolute local Unix-socket paths, and the exact disposable
fixture service `superplane-integration-test-postgres.superplane-integration-test.svc.cluster.local`
qualify. The fixture's existing apply wrapper separately enforces a loopback
kind/k3d/minikube cluster; the DNS name alone does not prove cluster locality.
Other Kubernetes services and similarly named hosts do not qualify. A missing
host is refused because ambient `PGHOST` could otherwise select a remote server.
The service, migration and installation paths pass their actual configured URL
to this check; a separate local `DATABASE_URL` environment value cannot disguise
a remote URL supplied to the connection. Verified connections with a configured
CA keep their existing behavior.
