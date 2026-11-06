# Isolated DevOps investigation fixture

This fixture supplies actual synthetic failure and recovery telemetry for the
[Lambda resource-verification path](devops-resource-verification.md). It is
separate from the queue-only release-verification fixture. Its preparation,
installation, telemetry verification, and native investigation are separate
outcomes. None can substitute for the next.

## Isolation and roles

The independent Terraform root is
`modules/L5-services/infra/devops-investigation-fixture/`. It creates one Lambda,
one precreated log group with one-day retention, an isolated VPC/subnet/security
group, and two roles with their inline policies. There are no triggers, queue
mappings, function URLs, network endpoints, internet routes, or secrets.

The three relevant roles remain distinct:

| Role | Authority |
| --- | --- |
| Existing ADP connection | Temporary setup, then exact resource verification through owner consent and the broker |
| Lambda execution | Only `logs:CreateLogStream` and `logs:PutLogEvents` on its precreated log group after sealing |
| Native resource reader | Only `lambda:GetFunction` on the selected Lambda and `logs:FilterLogEvents` on its exact log group |

The native reader trusts only `aidevops.amazonaws.com`, the selected account,
and the actual `agentspace/` ARN. No wildcard space or placeholder ID is used.
It has one inline policy, no managed policies or permission boundary, as
required by the current resource verifier.

Bootstrap permits temporary ENI provisioning for Lambda; the function itself
is explicitly denied those EC2 operations. Reserved concurrency is zero and no
numeric version is published. The sealed phase removes the provisioning
authority, publishes the selected code, and keeps concurrency zero. Activation
changes concurrency to one, retaining the sealed policy, 128 MiB memory and a
five-second timeout. Both VPC DNS settings are disabled; the security group has
no ingress or egress. Logging is performed by the Lambda service without giving
the function a network route.

## Prepare and review the connection policy

`infra/provider-access/` intentionally supports existing DevOps spaces and does
not grant creation or association authority. Do not broaden that root or add
an Allow beneath the dedicated fixture role's existing native Deny.

`scripts/prepare-devops-fixture-access.py` provides a separate offline policy
preparer. Its `snapshot_role` function accepts an injected operator IAM client
and reads only `GetRole`, `ListAttachedRolePolicies`, `ListRolePolicies`, and
`GetRolePolicy`. It rejects incomplete listings, managed policies, boundaries,
or more than the selected inline policy. Keep its output private: trust records
contain the existing API-generated delegation conditions.

The version-1 configuration has `stage`, `account_id`, `region`,
`connection_role_arn`, `expires_at`, and the full retained `setup_intent`.
Expiry must be future and at most one hour away. The create intent must be an
existing purpose-specific DevOps setup intent, including its original scope,
request, authorization, generation, and intact intent hash. The preparer derives
the binding UUID and `adp:scope` fingerprint using the runtime's algorithm.
It cannot manufacture a durable operation or prove that a supplied record is
still current.

For `stage: "setup"`, the existing policy must exactly match the deliberate
initial native-deny policy. The proposed replacement permits only tagged-space
creation, tag-bound space/goal reads, and regional space discovery. It omits
`UpdateGoal`: if a new space has an enabled goal, setup remains blocked for
separate review. AWS IAM cannot express a disable-only `UpdateGoal` grant.

For `stage: "resources"`, supply the previous setup plan, the new complete role
snapshot, the prepared fixture manifest as `fixture`, and actual `GetAgentSpace`
output as `native_readback`. Role ID, trust digest, prior policy, binding,
fingerprint, native ID/name/tag, and fixture role/resource boundaries must all
match. This replacement removes creation authority. It permits only exact-space
reads and AWS association, exact reader-role inspection/simulation and PassRole
to DevOps Agent, and the selected Lambda/log reads. Other operations are
explicitly denied.

The CLI accepts `--config`, `--snapshot`, optional `--previous-plan`, and
`--output`. It refuses output overwrite and writes a private plan containing
before/after policies, snapshot/trust/input hashes and review prerequisites.
It does not apply anything. Before replacing that single inline policy, the
operator re-reads the complete snapshot, matches its digest, and verifies
current Product ownership, disabled configuration, consent and retained work.
Preserve role trust and its exact ExternalId/owner conditions. Read back and
simulate the replacement before allowing work to consume it. A fresh plan is
required after any drift or expiry. To review another bounded window for the
same stage, provide its exact previous applied plan and a new complete snapshot;
the preparer preserves the operation, binding, scope and trust. A resource-stage
policy cannot be converted back into creation authority.

## Install and observe the fixture

1. Retain a disabled DevOps create operation through the supported owner API.
   Prepare/review its connection-policy setup stage from that operation, then
   complete native setup and retain the actual native space readback.
2. Run `scripts/prepare-devops-investigation-fixture.py` with `--config` and a
   new private `--output` directory. Its exact version-1 input keys are
   `schema_version`, `account_id`, `region`, `repository`, `commit`, `fixture_id`,
   and `native_space_id`. Pin a full Git SHA containing the reviewed fixture.
   The preparer reads the handler and all three Terraform files from that SHA,
   creates a deterministic ZIP, and hashes every prepared file. It makes no AWS
   calls. The manifest records `deployed: false` and `native_submissions: 0`.
3. Confirm the operator account and compare it with the pinned provider account.
   Initialize the independent private local backend using `backend.json`.
   Save and review the bootstrap plan using `bootstrap.tfvars.json`; apply only
   that reviewed plan. Inspect actual Lambda/network state, then save and review
   the sealed plan using `sealed.tfvars.json`.
4. Read Terraform's `fixture` output and call
   `devops_investigation_readback.verify_isolation` with injected IAM, EC2 and
   CloudWatch Logs clients. It verifies the exact execution-role trust, sole
   sealed policy, no managed policies/boundary, subnet/security group,
   local-only routes, disabled DNS, and precreated log-group retention.
   Missing or changed readback fails verification.
5. Review/apply `active.tfvars.json`. Explicitly invoke only the published
   numeric version, once with `dependency_timeout.json` and once with
   `recovered.json`. The first intentionally fails with
   `SYNTHETIC_DEPENDENCY_TIMEOUT`; the second returns `SYNTHETIC_RECOVERY`.
   Inputs contain only schema, exact correlation ID, and scenario. Extra input
   keys or arbitrary correlation values are rejected before logging.
6. `devops_investigation_readback.verify` reads both the published and current
   unqualified Lambda, its exact code hash/configuration and bounded
   concurrency, then actual logs from a selected window of at most five
   minutes. Both correlated markers must come from the numeric version's log
   stream. It retains event metadata, not raw logs. Neither readback helper
   invokes Lambda, changes infrastructure, or submits native work.
7. Prepare/review the connection-policy resources stage. Complete current
   resource-verification owner consent and the durable binding check, select
   its authentic workload/telemetry references, then explicitly activate and
   authorize one bounded manual investigation. References expire after fifteen
   minutes; refresh through the documented disabled-configuration repair path.

These steps contain no browser or AWS Console automation. Fixture observation
reports `native_qualified: false`. Completion requires the actual native result
and the separate qualification evidence. An empty log group, prepared archive,
successful plan, or simulated permission result is not native acceptance.

After use, return concurrency to zero, retain the evidence, revoke temporary
consent and connection authority, and review cleanup of only the recorded
fixture resources. Do not silently restore bootstrap EC2 permissions to delete
network interfaces; if Lambda cleanup requires that authority, review that
specific temporary cleanup plan. Native-space cleanup remains a separate
owned-resource operation.
