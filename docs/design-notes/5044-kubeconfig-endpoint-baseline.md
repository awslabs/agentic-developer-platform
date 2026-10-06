# Kubeconfig endpoint authorization: historical and current HTTP evidence

This is source-level, credential-free evidence for the missing R6.1 endpoint
regression in #5044. It does not establish a deployed authorization setting,
provider access, or closure of the other R5/R6 criteria.

## Provenance and method

- Historical API source: commit `a84d7a03bc409ecea3d3921ee66afabd25e934f0`, the parent of the domain-authorization integration commit `4bb0489ce49fecbcc502e19305e1fde6ba5257c3`. The test runs `git archive` at that immutable revision, loads its actual `app.main` mounted FastAPI app and `POST /workspaces/{workspace_id}/kubeconfig` handler in a separate Python process, and sends HTTP requests through `httpx.ASGITransport`. The handler's organization-only workspace lookup and kubeconfig serialization are unmodified.
- The historical application imported `python-jose`, which the maintained API removed because it pulls in an unfixed vulnerable dependency. The historical subprocess adapts only the old `jose.JWTError` and `jose.jwt` import to the already installed PyJWT implementation. Tokens remain signed and verified, but the test does **not** exercise historical `python-jose` verification or a historical deployment. It measures the old mounted handler's authorization decision with that explicit compatibility substitution.
- The old test configuration supplies in-memory SQLite. The fixture sets two organization rows, one active workspace and one fictional cluster, and issues distinct same-organization user tokens. STS assumption and EKS CA lookup are doubled at their call boundaries. A blocked socket connection and disabled AWS metadata/profile lookup prevent provider traffic. No cluster credentials are stored in the evidence; the returned kubeconfig describes an `aws eks get-token` exec plugin rather than containing a bearer token.
- The maintained endpoint uses the existing isolated SQLite test configuration, a signed RS256 access token, an explicit workspace grant and the real mounted global domain guard. The same organization has an ungranted principal, a read-only principal and a provision-granted principal. The corresponding STS/EKS calls are doubled; the guard must deny before either double is reached.

| Mounted HTTP request | Historical source | Current source |
|---|---:|---:|
| Same-org user without a workspace grant | 200, serialized kubeconfig | 403, no provider call |
| Other-org user | 404, no provider call | 403, no provider call |
| Designated owner identity (provision grant only in current source) | 200, serialized kubeconfig | 200, serialized kubeconfig |
| Read-only user in the workspace | No historical grant model | 403, no provider call |
| Missing bearer or spoofed identity header | Not part of historical comparison | 401 / 403, no provider call |

The historical case is **the known-failing policy baseline**, not a deliberately
failing test: asserting HTTP 200 demonstrates the old authorization defect. A
historical workspace membership record cannot be seeded because that revision
had no workspace-grant table. The distinct signed user identifiers, shared
organization and absence of any workspace-membership check establish the
same-org nonmember case.

## Transport boundary

The Gateway's public allowlist contains POST, not GET, for the kubeconfig route;
its focused HTTP test sends the same path, checks that only the bearer is
forwarded, denies unauthenticated and alternate-method/private-path requests,
and preserves a **simulated** 403 from the domain upstream. The mounted current
API classifies that POST as `workspace:provision` and tests its real response
with denied and granted principals. The CLI contract suite executes the
`workspace kubeconfig` command against a doubled Gateway client and verifies
POST and expiry. A source inventory confirms the current UI and MCP tool code
advertise no separate kubeconfig exporter. These separately tested boundaries
are not a single deployed Gateway-to-API HTTP test, and absence of an exporter
in current source is not proof against future or unreviewed transports.

## Reproduce offline

Prerequisites: a full Git checkout containing the historical commit, Python
3.12 or newer, and the repository's API and Gateway development dependencies
installed in **separate isolated environments**. Use the maintained domain API
CI job's installation list and `releases/transfer-constraints.txt` for the API;
that file pins the FastAPI version used by its route-introspection tests. No
AWS account, database server, credentials or test service is needed for the
commands below. The Gateway environment must not inherit an operator profile.

API environment (activate its isolated Python environment first):

```sh
cd modules/domain-apps/superplane/src/superplane-api
python -m pytest tests/test_kubeconfig_endpoint_history.py tests/test_kubeconfig_endpoint_current.py tests/test_auth.py -q
```

Gateway environment (activate its separate isolated Python environment first):

```sh
cd modules/gateway
env -i PATH="$PATH" HOME=/tmp AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null python -m pytest tests/features/test_superplane_proxy.py tests/cli/test_superplane_contract.py -q
```

The broader relevant checks additionally run the offline API suite without
PostgreSQL-specific tests, and the maintained Gateway auth suite. PostgreSQL,
live target configuration and final-head CI are separate evidence; do not
infer their results from these commands. R5.5 still requires a target-specific
record of `ENFORCE_CREDENTIAL_BINDING` and
`VAULT_ENFORCE_CREDENTIAL_HOST_BINDING`, which this offline regression cannot
supply. Foreground owns review and any eventual issue-closure decision.
