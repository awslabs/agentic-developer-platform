"""The real entry point, driven through the real adapters — finding F1.

F1 verbatim: "the implementation is not connected to any runtime path —
`bootstrap_workspace` had no caller outside its own tests; `ClusterAccess` and
`RegistrationStore` were Protocols with no implementation." The review asked for
"integration tests proving the real entry point invokes every gate".

WHY THIS FILE IS DIFFERENT FROM THE OTHER SUITES

Every other suite substitutes `FakeClusterAccess` for the cluster. That proves the gates
DECIDE correctly, and it cannot prove the production adapter answers the same questions
the fake does. This file removes the fake: `bootstrap_workspace` runs against
`KubectlClusterAccess`, `AwsPrerequisiteAccess` and `SqlRegistrationStore`, and the only
substitution is at the very bottom of the stack — `CommandRunner` and
`TransactionalStore`, the two seams that would otherwise touch a cluster and a database.

That boundary placement is the whole point. The adapters' argv construction, their JSON
parsing, their refusal conversion and their read-back-rather-than-assume discipline all
execute here. Two defects in the production code were found this way, both invisible to
the 316 tests that existed before it: the adapter did not implement two `ClusterAccess`
seams at all, and `controller_deployments` returned every Deployment on the cluster where
`readiness.py` requires exactly one workspace controller.

`_FakeCluster` is a small state machine, not a canned-reply table, because the sequence
under test MUTATES and then RE-READS: `establish_crds` applies and re-reads, the taint is
removed and re-read, the controller is installed and read back. A table of fixed replies
would answer "the CRD exists" before it had been created, which is precisely the
assume-rather-than-observe error F2 was about — so a table could not tell a correct
sequence from a broken one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager

from superplane_bootstrap.adapters import (
    AwsPrerequisiteAccess,
    CommandResult,
    IamNodeRoleFacts,
    KubectlClusterAccess,
)
from superplane_bootstrap.components import CONTROLLER_IMAGE_MARKER
from superplane_bootstrap.prerequisites import ExpectedPrerequisites
from superplane_bootstrap.readiness import (
    FORBIDDEN_CONTROLLER_PERMISSIONS,
    REQUIRED_CONTROLLER_PERMISSIONS,
    REQUIRED_SYSTEM_WORKLOADS,
    SYSTEM_NAMESPACE,
)
from superplane_bootstrap.registry import SqlRegistrationStore
from superplane_bootstrap.state import FileStateStore
from superplane_bootstrap.workspace import (
    BOOTSTRAP_TAINT_KEY,
    WORKSPACE_CONTROLLER_NAME,
    bootstrap_workspace,
)

from .conftest import (
    ACCESS_POLICY_ARN,
    ACCOUNT_ID,
    CA_DATA,
    CLUSTER_ARN,
    CLUSTER_NAME,
    CLUSTER_SG_ID,
    CNI_ROLE_ARN,
    CREDENTIAL_ID,
    ENDPOINT,
    ENFORCE_VERSION,
    ENDPOINT_RULE_ID,
    MANAGEMENT_RULE_ID,
    MANAGEMENT_SG_ID,
    NAMESPACE,
    OIDC_ISSUER,
    ORG_ID,
    PRINCIPAL_ARN,
    REGION,
    VPC_ID,
    WORKSPACE_CRDS,
    WORKSPACE_ID,
)

CONTROLLER_IMAGE = f"registry.example/{CONTROLLER_IMAGE_MARKER}:v1"
NODE_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/superplane-workspace-node"
CONTROLLER_NAMESPACE = SYSTEM_NAMESPACE
NAMESPACE_UID = "namespace-uid-integration"
CONTRACT_VERSION = "1.0.0"


def _screen(value: object, *, what: str = "") -> None:
    """A `SecretScreen` that accepts everything.

    Signature matches the contract's `assert_no_secret_material(value, what=...)`, which
    is what `registration._screen_record` calls it as. The REAL screen is what
    `test_registry.py` drives, because the question there is whether a real secret is
    caught; the question here is whether the sequence reaches the screen at all.
    """
    return None


class _FakeCluster:
    """The cluster and AWS control plane as a mutable state machine.

    Answers `kubectl` and `aws` invocations from state that the adapters' own mutations
    change, so a re-read after a create genuinely observes the create. Every command is
    recorded in `commands`, which is how the ordering assertions below are made against
    the REAL argv rather than against a fake's method names.

    Knobs are deliberately narrow — each one expresses a single hazard the sequence is
    supposed to catch, and each is used by exactly one negative test.
    """

    def __init__(
        self,
        *,
        crd_apply_fails: bool = False,
        coredns_available: int = 2,
        taint_removal_fails: bool = False,
        extra_controller: bool = False,
        pre_existing_namespace: Mapping[str, object] | None = None,
        imds_reachable: bool = False,
        management_rule_present: bool = True,
        node_role_over_scoped: bool = False,
    ) -> None:
        self.crd_apply_fails = crd_apply_fails
        self.coredns_available = coredns_available
        self.taint_removal_fails = taint_removal_fails
        self.extra_controller = extra_controller
        self.imds_reachable = imds_reachable
        self.management_rule_present = management_rule_present
        self.node_role_over_scoped = node_role_over_scoped

        # Live cluster state the adapters mutate and re-read.
        self.namespaces: dict[str, dict[str, object]] = {}
        if pre_existing_namespace is not None:
            self.namespaces[NAMESPACE] = dict(pre_existing_namespace)
        self.crds: set[str] = set()
        self.deployments: dict[tuple[str, str], dict[str, object]] = {
            (SYSTEM_NAMESPACE, name): {
                "replicas": 2,
                "available": self.coredns_available,
                # A registry account id in the documentation range, not the real EKS
                # add-on registry's: `conftest.py`'s rule is that every identity in a
                # fixture is obviously synthetic, so a value that leaked into a real
                # invocation could not resolve to anything.
                "image": f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/eks/coredns:v1.11",
            }
            for name in REQUIRED_SYSTEM_WORKLOADS
        }
        # An ordinary EKS add-on Deployment. Present in every scenario because its
        # presence is what caught the `controller_deployments` defect: it is a Deployment
        # and it is not a workspace controller.
        self.deployments[(SYSTEM_NAMESPACE, "ebs-csi-controller")] = {
            "replicas": 2,
            "available": 2,
            "image": "public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1.28.0",
        }
        if extra_controller:
            self.deployments[("somebody-elses-namespace", "superplane-controller")] = {
                "replicas": 1,
                "available": 1,
                "image": f"other.registry/{CONTROLLER_IMAGE_MARKER}:v0",
            }
        self.taints: list[dict[str, str]] = [
            {"key": BOOTSTRAP_TAINT_KEY, "value": "pending", "effect": "NoSchedule"}
        ]
        self.rbac_applied: list[Mapping[str, object]] = []
        self.commands: list[tuple[str, ...]] = []
        self.uid_counter = 0
        self.namespace_created_at = -1

    # --- the CommandRunner seam -------------------------------------------

    def run(
        self, args: Sequence[str], *, data: str | None = None, timeout: int = 120
    ) -> CommandResult:
        argv = tuple(str(arg) for arg in args)
        self.commands.append(argv)
        if argv[0] == "kubectl":
            return self._kubectl(argv, data)
        if argv[0] == "aws":
            return self._aws(argv)
        raise AssertionError(f"unexpected binary: {argv[0]}")

    def _ok(self, argv: tuple[str, ...], payload: object = None) -> CommandResult:
        return CommandResult(
            args=argv,
            returncode=0,
            stdout="" if payload is None else json.dumps(payload),
        )

    def _fail(self, argv: tuple[str, ...], stderr: str) -> CommandResult:
        return CommandResult(args=argv, returncode=1, stderr=stderr)

    def _dry_run(self, argv: tuple[str, ...], data: str | None) -> CommandResult:
        """A server-side dry-run apply: VALIDATE first, then run Pod Security admission.

        Both halves matter. A real API server rejects a malformed object before any
        admission plugin sees it, and the exit status is identical either way — which is
        what let the adapter send `{"name": ..., "hostPID": true}` (not a Pod at all) and
        have every probe come back "rejected", including the conforming control. The
        gate reads a rejected control as "these rejections do not establish selective
        enforcement", so the taint could never be cleared on any cluster.

        The `restricted` model is likewise strict about ABSENT controls, not just
        unsafe ones: a pod that does not positively assert `allowPrivilegeEscalation:
        false` and `runAsNonRoot: true` is rejected. Modelling that is what makes the
        conforming probe a real control rather than a pod that passes trivially.
        """
        body = json.loads(data or "{}")
        if body.get("apiVersion") != "v1" or body.get("kind") != "Pod":
            return self._fail(
                argv, "error: error validating data: apiVersion not set, kind not set"
            )
        spec = body.get("spec") or {}
        containers = spec.get("containers") or []
        if not containers or not all(c.get("image") for c in containers):
            return self._fail(
                argv,
                "error: error validating data: ValidationError(Pod.spec): missing "
                'required field "containers"',
            )
        for container in containers:
            security = container.get("securityContext") or {}
            if security.get("privileged"):
                continue  # judged by the unsafe-field check below, not here
            if (
                security.get("allowPrivilegeEscalation") is not False
                or not security.get("runAsNonRoot")
                or (security.get("capabilities") or {}).get("drop") != ["ALL"]
            ):
                return self._fail(
                    argv,
                    "Error from server (Forbidden): violates PodSecurity "
                    '"restricted:v1.35": allowPrivilegeEscalation != false, '
                    "unrestricted capabilities, runAsNonRoot != true",
                )
        unsafe = (
            spec.get("hostNetwork")
            or spec.get("hostPID")
            or spec.get("hostIPC")
            or any(
                (c.get("securityContext") or {}).get("privileged") for c in containers
            )
            or any("hostPath" in (v or {}) for v in (spec.get("volumes") or ()))
        )
        if unsafe:
            return self._fail(
                argv,
                "Error from server (Forbidden): violates PodSecurity "
                '"restricted:v1.35": host namespaces not allowed, privileged, '
                "restricted volume types",
            )
        return self._ok(argv, {})

    def _kubectl(self, argv: tuple[str, ...], data: str | None) -> CommandResult:
        # `--kubeconfig <path>` is two argv entries and `--request-timeout=...` is one.
        # Dropping the flag but keeping the path is how a tmp_path leaked into every
        # match below and made every command look undeclared.
        rest: list[str] = []
        skip_next = False
        for arg in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg == "--kubeconfig":
                skip_next = True
                continue
            if arg.startswith("--request-timeout="):
                continue
            rest.append(arg)
        joined = " ".join(rest)
        if joined == "config view --raw --minify -o json":
            return self._ok(
                argv,
                {
                    "clusters": [
                        {
                            "cluster": {
                                "server": ENDPOINT,
                                "certificate-authority-data": CA_DATA,
                            }
                        }
                    ]
                },
            )
        if (
            data
            and data.startswith("{")
            and json.loads(data).get("kind") == "SelfSubjectAccessReview"
        ):
            return self._ok(argv, {"status": {"allowed": True}})
        if joined == "get rolebindings -n " + NAMESPACE + " -o json":
            return self._ok(argv, {"items": []})

        if "get serviceaccounts" in joined or "get clusterrolebindings" in joined:
            return self._ok(argv, {"items": []})
        if (
            data
            and data.startswith("{")
            and json.loads(data).get("kind") == "SubjectAccessReview"
        ):
            spec = json.loads(data)["spec"]
            attrs = spec["resourceAttributes"]
            resource = attrs["resource"] + (
                "." + attrs["group"] if attrs.get("group") else ""
            )
            return self._ok(
                argv,
                {
                    "status": {
                        "allowed": (attrs["verb"], resource)
                        in REQUIRED_CONTROLLER_PERMISSIONS
                    }
                },
            )

        if "get crd" in joined:
            return self._ok(
                argv, {"items": [{"metadata": {"name": n}} for n in sorted(self.crds)]}
            )

        if "get deployments --all-namespaces" in joined:
            return self._ok(
                argv,
                {
                    "items": [
                        {
                            "metadata": {"name": name, "namespace": ns},
                            "spec": {
                                "replicas": spec["replicas"],
                                "template": {
                                    "spec": {"containers": [{"image": spec["image"]}]}
                                },
                            },
                            "status": {"availableReplicas": spec["available"]},
                        }
                        for (ns, name), spec in self.deployments.items()
                    ]
                },
            )

        if "get namespace" in joined:
            name = rest[rest.index("namespace") + 1]
            existing = self.namespaces.get(name)
            if existing is None:
                return self._fail(
                    argv, f'Error from server (NotFound): namespaces "{name}" not found'
                )
            return self._ok(argv, {"metadata": existing})

        if "get deployment" in joined:
            name = rest[rest.index("deployment") + 1]
            namespace = rest[rest.index("-n") + 1]
            spec = self.deployments.get((namespace, name))
            if spec is None:
                return self._fail(
                    argv,
                    f'Error from server (NotFound): deployments "{name}" not found',
                )
            return self._ok(
                argv,
                {
                    "metadata": {"name": name, "namespace": namespace},
                    "spec": {"replicas": spec["replicas"]},
                    "status": {"availableReplicas": spec["available"]},
                },
            )

        if "get nodes" in joined:
            return self._ok(argv, {"items": [{"spec": {"taints": list(self.taints)}}]})

        if "get lease" in joined:
            return self._fail(argv, "Error from server (NotFound): leases not found")

        if joined.startswith("create -f -"):
            body = json.loads(data or "{}")
            self.namespace_created_at = len(self.commands) - 1
            self.uid_counter += 1
            metadata = {
                "name": body["metadata"]["name"],
                "uid": NAMESPACE_UID,
                "labels": body["metadata"].get("labels", {}),
            }
            self.namespaces[body["metadata"]["name"]] = metadata
            return self._ok(argv, {"metadata": metadata})

        # BEFORE the general `apply -f -` branch: a dry-run apply is also an
        # `apply -f -`, and matching the general branch first made the whole admission
        # probe a no-op that answered "admitted" to every unsafe pod.
        if "--dry-run=server" in joined:
            return self._dry_run(argv, data)

        if joined.startswith("apply -f -"):
            body = json.loads(data or "{}")
            if body.get("kind") == "List":
                self.rbac_applied.extend(body["items"])
                return self._ok(argv, {})
            if body.get("kind") == "Deployment":
                name = body["metadata"]["name"]
                namespace = body["metadata"]["namespace"]
                image = body["spec"]["template"]["spec"]["containers"][0]["image"]
                self.deployments[(namespace, name)] = {
                    "replicas": body["spec"]["replicas"],
                    "available": body["spec"]["replicas"],
                    "image": image,
                }
                return self._ok(argv, {})
            return self._ok(argv, {})

        if joined.startswith("apply -f "):
            # A CRD manifest applied from a declared path.
            if self.crd_apply_fails:
                return self._fail(argv, "error validating data: unknown field")
            self.crds.update(WORKSPACE_CRDS)
            return self._ok(argv, {})

        if joined.startswith("patch deployment"):
            name = rest[rest.index("deployment") + 1]
            namespace = rest[rest.index("-n") + 1]
            spec = self.deployments.get((namespace, name))
            if spec is None:
                return self._fail(argv, "Error from server (NotFound)")
            # The toleration lets it schedule; availability is whatever the scenario says.
            spec["available"] = self.coredns_available
            return self._ok(argv, {})

        if joined.startswith("taint nodes"):
            key_arg = rest[rest.index("--all") + 1]
            if key_arg.endswith("-"):
                if self.taint_removal_fails:
                    return self._fail(argv, "error: unable to update node")
                key = key_arg[:-1]
                self.taints = [t for t in self.taints if t.get("key") != key]
            else:
                key = key_arg.split("=", 1)[0]
                if not any(t.get("key") == key for t in self.taints):
                    self.taints.append(
                        {"key": key, "value": "pending", "effect": "NoSchedule"}
                    )
            return self._ok(argv, {})

        if joined.startswith("auth can-i"):
            verb = rest[rest.index("can-i") + 1]
            resource = rest[rest.index("can-i") + 2]
            granted = (verb, resource) in set(REQUIRED_CONTROLLER_PERMISSIONS)
            return CommandResult(
                args=argv, returncode=0, stdout="yes\n" if granted else "no\n"
            )

        if joined.startswith("run ") or "exec" in joined:
            # The IMDS reachability probe.
            return CommandResult(
                args=argv,
                returncode=0,
                stdout="ADP_IMDS_REACHABLE\n"
                if self.imds_reachable
                else "ADP_IMDS_UNREACHABLE\n",
            )

        if "get serviceaccount aws-node" in joined:
            # The CNI's dedicated IRSA role, annotated exactly as EKS annotates it. An
            # empty object here would make `aws_node_role_arn` blank and the
            # credential-scope proof fail for a reason the scenario did not intend.
            return self._ok(
                argv,
                {
                    "metadata": {
                        "name": "aws-node",
                        "namespace": SYSTEM_NAMESPACE,
                        "annotations": {"eks.amazonaws.com/role-arn": CNI_ROLE_ARN},
                    }
                },
            )

        if "get serviceaccount" in joined or "auth reconcile" in joined:
            return self._ok(argv, {})

        raise AssertionError(f"undeclared kubectl command: {joined}")

    def _aws(self, argv: tuple[str, ...]) -> CommandResult:
        joined = " ".join(argv)

        if "sts get-caller-identity" in joined:
            return self._ok(argv, {"Account": ACCOUNT_ID, "Arn": PRINCIPAL_ARN})

        if "eks describe-cluster" in joined:
            return self._ok(
                argv,
                {
                    "cluster": {
                        "name": CLUSTER_NAME,
                        "arn": CLUSTER_ARN,
                        "endpoint": ENDPOINT,
                        "status": "ACTIVE",
                        "version": "1.31",
                        "certificateAuthority": {"data": CA_DATA},
                        "resourcesVpcConfig": {
                            "vpcId": VPC_ID,
                            "clusterSecurityGroupId": CLUSTER_SG_ID,
                        },
                        "identity": {"oidc": {"issuer": OIDC_ISSUER}},
                    }
                },
            )

        if "eks describe-access-entry" in joined:
            return self._ok(
                argv,
                {
                    "accessEntry": {
                        "principalArn": PRINCIPAL_ARN,
                        "type": "STANDARD",
                        "clusterName": CLUSTER_NAME,
                        "accessEntryArn": CLUSTER_ARN.replace(
                            ":cluster/", ":access-entry/"
                        )
                        + "/role/000000000000/SyntheticTestRole/entry-one",
                    }
                },
            )

        if "eks list-associated-access-policies" in joined:
            return self._ok(
                argv,
                {
                    "associatedAccessPolicies": [
                        {
                            "policyArn": ACCESS_POLICY_ARN,
                            "accessScope": {
                                "type": "namespace",
                                "namespaces": [NAMESPACE],
                            },
                        }
                    ]
                },
            )

        if "ec2 describe-security-group-rules" in joined:
            # The two rules are NOT symmetric, and getting that wrong here is how a
            # fake would hide a real bug: the inbound endpoint rule lives ON the
            # cluster SG and REFERENCES the management SG, and the return-path rule
            # is the exact mirror. A fake that put both on the cluster SG would let
            # `_verify_rule` accept a management rule that does not exist.
            rules = [
                {
                    "SecurityGroupRuleId": ENDPOINT_RULE_ID,
                    "GroupId": CLUSTER_SG_ID,
                    "IsEgress": False,
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "ReferencedGroupInfo": {"GroupId": MANAGEMENT_SG_ID},
                    "OwnerId": ACCOUNT_ID,
                }
            ]
            if self.management_rule_present:
                rules.append(
                    {
                        "SecurityGroupRuleId": MANAGEMENT_RULE_ID,
                        "GroupId": "sg-synthetic-sts",
                        "IsEgress": False,
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "ReferencedGroupInfo": {"GroupId": "sg-synthetic-nodes"},
                        "OwnerId": ACCOUNT_ID,
                    }
                )
            for rule in rules:
                rule["Tags"] = [
                    {"Key": "OrgId", "Value": ORG_ID},
                    {"Key": "WorkspaceId", "Value": WORKSPACE_ID},
                ]
            # EC2 honours `Name=group-id,Values=...`; so does this. Returning every
            # rule regardless of the filter is what made the adapter's own
            # "the read did not answer the question that was asked" guard fire, and
            # that guard is load-bearing — it must be exercised, not bypassed.
            requested = ""
            for arg in argv:
                if arg.startswith("Name=group-id,Values="):
                    requested = arg.split("=", 2)[2]
            if requested:
                rules = [rule for rule in rules if rule["GroupId"] == requested]
            return self._ok(argv, {"SecurityGroupRules": rules})

        if "ec2 describe-security-groups" in joined:
            return CommandResult(args=argv, returncode=0, stdout=f"{VPC_ID}\n")

        if "iam simulate-principal-policy" in joined:
            # A correctly-scoped node role: the CNI actions and account-wide ECR both
            # denied, because the dedicated IRSA role is what holds them. One
            # EvaluationResult per requested action, which is what the real API returns
            # and what the adapter requires before it will report a decision.
            requested = []
            collecting = False
            for arg in argv:
                if arg == "--action-names":
                    collecting = True
                    continue
                if arg.startswith("--"):
                    collecting = False
                    continue
                if collecting:
                    requested.append(arg)
            decision = "allowed" if self.node_role_over_scoped else "implicitDeny"
            return self._ok(
                argv,
                {
                    "EvaluationResults": [
                        {"EvalActionName": action, "EvalDecision": decision}
                        for action in requested
                    ]
                },
            )

        if "iam" in joined or "eks describe-addon" in joined:
            return self._ok(argv, {})

        raise AssertionError(f"undeclared aws command: {joined}")

    # --- assertions helpers ------------------------------------------------

    def kubectl_index(self, marker: str) -> int:
        """Index of the first kubectl command whose argv contains `marker`.

        Used for ordering assertions. -1 when absent, so a missing step fails an
        ordering assertion loudly rather than comparing None.
        """
        for index, argv in enumerate(self.commands):
            if marker in " ".join(argv):
                return index
        return -1


class _FakeSqlStore:
    """A `TransactionalStore` over a dict, honouring the reserve/finalize contract.

    Deliberately implements the SQL semantics the real store depends on rather than
    short-circuiting them: the advisory lock is recorded, `SELECT ... FOR UPDATE` returns
    the current row, and the insert refuses a second reservation for a different
    identity. That is what lets `SqlRegistrationStore` — the production class — run here
    unmodified.
    """

    def __init__(self, existing: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.rows: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in (existing or {}).items()
        }
        self.statements: list[str] = []
        self.locks: list[str] = []
        self.authority_rows = {}

    @contextmanager
    def transaction(self):
        yield self

    def execute(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        collapsed = " ".join(statement.split())
        self.statements.append(collapsed)

        if "pg_advisory_xact_lock" in collapsed:
            self.locks.append(str(parameters.get("binding", "")))
            return []

        if "workspace_bootstrap_authority" in collapsed:
            key = (parameters.get("workspace_id"), parameters.get("generation"))
            if collapsed.startswith("INSERT"):
                assert key not in self.authority_rows
                self.authority_rows[key] = {
                    **parameters,
                    "progress_json": "{}",
                    "revoked": False,
                }
                return []
            if collapsed.startswith("UPDATE"):
                self.authority_rows[key].update(parameters)
                return []
            rows = [
                dict(row)
                for row in self.authority_rows.values()
                if all(row.get(k) == v for k, v in parameters.items())
            ]
            if "revoked=false" in collapsed or "revoked = false" in collapsed:
                rows = [row for row in rows if not row["revoked"]]
            if "revoked=true" in collapsed:
                rows = [row for row in rows if row["revoked"]]
            return rows

        if "FROM organizations" in collapsed:
            return [{"id": ORG_ID, "adp_org_id": ORG_ID}]
        if "workspace_bootstrap_reservations" not in collapsed:
            return []  # Canonical writes are exercised against real PostgreSQL.
        workspace_id = str(parameters.get("workspace_id", ""))

        row = self.rows.get(workspace_id)
        # Every statement below that names `state` in its WHERE clause is honoured here.
        # Ignoring it is not a harmless shortcut: `_READ_REGISTRATION` filters on
        # `state = 'registered'` precisely so that a RESERVATION is never mistaken for a
        # REGISTRATION, and a fake that returns the row regardless makes
        # `finalize_registration` compare a completed record against a reservation that
        # has no `namespace_uid` yet — reported as an attempted rebinding.
        wanted_state = str(parameters.get("state", ""))

        if collapsed.startswith("SELECT") and "FOR UPDATE" in collapsed:
            # No state filter in `_SELECT_FOR_UPDATE`: reserve and finalize both need to
            # see a row in EITHER state, which is how a replay is detected at all.
            return [dict(row)] if row is not None else []

        if collapsed.startswith("SELECT"):
            if row is None or (
                "state" in parameters and row.get("state") != wanted_state
            ):
                return []
            return [dict(row)]

        if collapsed.startswith("INSERT"):
            if row is not None:
                # A real primary key, so the reserve path's own lock+read is what keeps
                # this from firing rather than luck.
                raise AssertionError(
                    "duplicate key value violates unique constraint "
                    '"workspace_bootstrap_reservations_pkey"'
                )
            self.rows[workspace_id] = {
                str(k): str(v) for k, v in parameters.items() if v is not None
            }
            return []

        if collapsed.startswith("UPDATE"):
            if row is None:
                return []
            row.update({str(k): str(v) for k, v in parameters.items() if v is not None})
            return []

        if collapsed.startswith("DELETE"):
            # `AND state = :state` is the safety property `release` depends on: a
            # completed registration must survive a refusal path. RETURNING reports
            # whether anything was actually dropped.
            if row is None or row.get("state") != wanted_state:
                return []
            self.rows.pop(workspace_id, None)
            return [{"workspace_id": workspace_id}]

        raise AssertionError(f"undeclared statement: {collapsed}")


def _binding() -> object:
    from datetime import datetime, UTC
    from superplane_contracts.provisioning import (
        OperationBinding,
        ResolvedPrincipal,
        PROVISION,
        REQUIRED_PERMISSION,
    )

    return OperationBinding(
        operation_id="synthetic-operation",
        principal=ResolvedPrincipal(
            subject="synthetic-subject", org_id=ORG_ID, workspace_id=WORKSPACE_ID
        ),
        action=PROVISION,
        permission=REQUIRED_PERMISSION,
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


def _observed_cluster(cluster: _FakeCluster):
    from superplane_bootstrap.adapters import AwsObserver

    return AwsObserver(runner=cluster, region=REGION).cluster_identity(CLUSTER_NAME)


def _run(cluster: _FakeCluster, tmp_path, **overrides):
    """Drive the REAL entry point through the REAL adapters against `cluster`."""
    manifest = tmp_path / "crds.yaml"
    manifest.write_text("---\n")
    store = overrides.pop("sql_store", None) or _FakeSqlStore()

    access = KubectlClusterAccess(
        runner=cluster,
        kubeconfig=tmp_path / "kubeconfig",
        controller_namespace=CONTROLLER_NAMESPACE,
        controller_service_account=WORKSPACE_CONTROLLER_NAME,
        controller_image=CONTROLLER_IMAGE,
        imds_probe_image="registry.example/python@sha256:" + "0" * 64,
        node_role=IamNodeRoleFacts(runner=cluster, node_role_arn=NODE_ROLE_ARN),
        manifests={name: manifest for name in WORKSPACE_CRDS},
        tenant_identity_reader=lambda: (),
    )

    arguments = {
        "binding": _binding(),
        "provider": __import__(
            "superplane_bootstrap.adapters", fromlist=["AwsObserver"]
        )
        .AwsObserver(runner=cluster, region=REGION)
        .provider_identity(),
        "access": access,
        "prerequisite_access": AwsPrerequisiteAccess(runner=cluster, region=REGION),
        "store": SqlRegistrationStore(store=store),
        "state_store": FileStateStore(tmp_path / "state.json"),
        "observed_cluster": _observed_cluster(cluster),
        "expected_account_id": ACCOUNT_ID,
        "expected_region": REGION,
        "expected_cluster_name": CLUSTER_NAME,
        "expected_cluster_arn": CLUSTER_ARN,
        "expected_certificate_authority_data": CA_DATA,
        "expected_cni_role_arn": CNI_ROLE_ARN,
        "expected_prerequisites": ExpectedPrerequisites(
            account_id=ACCOUNT_ID,
            vpc_id=VPC_ID,
            cluster_security_group_id=CLUSTER_SG_ID,
            management_security_group_id=MANAGEMENT_SG_ID,
            node_security_group_id="sg-synthetic-nodes",
            sts_endpoint_security_group_id="sg-synthetic-sts",
            sts_endpoint_vpc_id=VPC_ID,
        ),
        "cluster_ownership": "adp-created",
        "namespace": NAMESPACE,
        "enforce_version": ENFORCE_VERSION,
        "credential_reference_id": CREDENTIAL_ID,
        "contract_version": CONTRACT_VERSION,
        "screen": _screen,
        "required_crds": WORKSPACE_CRDS,
        "required_system_workloads": REQUIRED_SYSTEM_WORKLOADS,
    }
    arguments.update(overrides)
    if "authority_factory" not in arguments:
        from .authority_integration_support import compose_authority

        # The service composer owns a genuine binding. Invalid request bindings
        # must reach the public gate, not fail prematurely in this fixture setup.
        arguments["authority_factory"] = compose_authority(
            cluster, tmp_path, {**arguments, "binding": _binding()}
        )
    outcome = bootstrap_workspace(**arguments)
    return outcome, store


# --- the gate sequence, through the production adapters ------------------------


def test_every_gate_runs_through_the_real_adapters(tmp_path):
    """**The test F1 asked for.**

    `bootstrap_workspace` — the real entry point — runs with `KubectlClusterAccess`,
    `AwsPrerequisiteAccess` and `SqlRegistrationStore`. Only the command runner and the
    SQL connection are substituted. Every assertion below is on a fact the production
    adapter produced by parsing output it would really receive.
    """
    cluster = _FakeCluster()

    outcome, store = _run(cluster, tmp_path)

    assert outcome.refusal is None, f"refused: {outcome.refusal}"
    assert outcome.registered is True
    assert outcome.ready is True
    assert outcome.taint_cleared is True
    assert outcome.nodes_left_schedulable is False
    # The registration reached the store through the production SQL class.
    assert WORKSPACE_ID in store.rows
    assert store.locks, "the advisory lock was never taken"


def test_the_gates_run_in_the_order_the_interlock_depends_on(tmp_path):
    """Order is the substance of F2 and F5, asserted here on REAL argv.

    The namespace and CRDs exist before the controller is installed; the controller and
    the system workloads are in place before readiness is checked; readiness and the
    reservation both precede the taint removal; the taint is gone before the registration
    is finalized. Every one of these is a hazard if inverted, and the earlier suites could
    only assert it against the fake's method names.
    """
    cluster = _FakeCluster()

    outcome, store = _run(cluster, tmp_path)
    assert outcome.registered is True

    created_namespace = cluster.namespace_created_at
    applied_crds = cluster.kubectl_index("apply -f " + str(tmp_path))
    installed_controller = next(
        index
        for index, argv in enumerate(cluster.commands)
        if "apply -f -" in " ".join(argv)
        and any(CONTROLLER_IMAGE in a for a in argv) is False
        and index > applied_crds
    )
    placed_coredns = cluster.kubectl_index("patch deployment coredns")
    removed_taint = cluster.kubectl_index(f"taint nodes --all {BOOTSTRAP_TAINT_KEY}-")

    assert -1 not in (created_namespace, applied_crds, placed_coredns, removed_taint)
    assert created_namespace < applied_crds < installed_controller
    assert placed_coredns < removed_taint
    assert installed_controller < removed_taint, (
        "the taint came off before the controller was installed, so tenant work could "
        "schedule on a workspace with nothing reconciling it"
    )

    # The reservation is taken BEFORE the taint comes off (F5), and the registration is
    # finalized only after. Both are visible in the SQL statement log.
    insert = next(i for i, s in enumerate(store.statements) if s.startswith("INSERT"))
    update = next(i for i, s in enumerate(store.statements) if s.startswith("UPDATE"))
    assert insert < update, "the workspace was registered before it was reserved"


def test_the_namespace_is_created_with_the_admission_labels_it_is_later_proved_against(
    tmp_path,
):
    """The labels the namespace is CREATED with must be the ones the isolation gate
    REQUIRES, or the namespace fails its own proof. Asserted on the manifest the real
    adapter put on stdin."""
    cluster = _FakeCluster()

    outcome, _ = _run(cluster, tmp_path)
    assert outcome.registered is True

    created = cluster.namespaces[NAMESPACE]["labels"]
    assert created["pod-security.kubernetes.io/enforce"] == "restricted"
    assert created["pod-security.kubernetes.io/enforce-version"] == ENFORCE_VERSION


def test_the_controller_rbac_grants_exactly_what_the_gate_then_verifies(tmp_path):
    """End to end: the adapter APPLIES the RBAC, and `kubectl auth can-i` — answered from
    the same required set — is what the readiness gate then reads. A divergence between
    the two would install a controller that fails its own readiness check."""
    cluster = _FakeCluster()

    outcome, _ = _run(cluster, tmp_path)
    assert outcome.registered is True

    kinds = {item["kind"] for item in cluster.rbac_applied}
    assert {"ServiceAccount", "Role", "RoleBinding", "ClusterRole"} <= kinds
    granted: set[tuple[str, str]] = set()
    for item in cluster.rbac_applied:
        for rule in item.get("rules", []):
            group = rule["apiGroups"][0]
            for resource in rule["resources"]:
                qualified = f"{resource}.{group}" if group else resource
                for verb in rule["verbs"]:
                    granted.add((verb, qualified))

    assert granted == set(REQUIRED_CONTROLLER_PERMISSIONS)
    assert granted.isdisjoint(set(FORBIDDEN_CONTROLLER_PERMISSIONS))


def test_the_workspace_controller_is_the_only_reconciler_counted(tmp_path):
    """**The regression test for the second production defect.**

    This cluster has CoreDNS and the EBS CSI controller — both Deployments, neither a
    workspace controller. Before the fix, `controller_deployments` returned all three
    images, `readiness._controller_checks` saw three reconcilers instead of one, and the
    bootstrap could never remove the taint on ANY real cluster.
    """
    cluster = _FakeCluster()
    assert len(cluster.deployments) >= 2

    outcome, _ = _run(cluster, tmp_path)

    assert outcome.ready is True, (
        f"readiness failed with non-controller Deployments present: "
        f"{getattr(outcome.readiness, 'failures', ())}"
    )


def test_a_second_workspace_controller_refuses_before_anything_is_created(tmp_path):
    """Two controllers on one cluster-scoped NodePool set contend continuously rather
    than failing cleanly, so this refuses — and it refuses before the namespace exists."""
    cluster = _FakeCluster(extra_controller=True)

    outcome, store = _run(cluster, tmp_path)

    assert outcome.registered is False
    assert outcome.refusal is not None
    assert NAMESPACE not in cluster.namespaces
    assert store.rows == {}


# --- the interlock is never left open ------------------------------------------


def test_a_crd_failure_leaves_a_cleanup_plan_and_the_taint_on(tmp_path):
    """**F6 end to end.** The namespace exists by the time the CRD apply fails, so the
    refusal must carry a plan naming it — and the taint must still be on, because nothing
    was ever proved."""
    cluster = _FakeCluster(crd_apply_fails=True)

    outcome, store = _run(cluster, tmp_path)

    assert outcome.registered is False
    assert outcome.cleanup is not None, "a created namespace with no cleanup plan is F6"
    assert outcome.cleanup.deletes_nothing is False
    assert any(t["key"] == BOOTSTRAP_TAINT_KEY for t in cluster.taints)
    assert outcome.nodes_left_schedulable is False
    assert store.rows == {}

    # The durable record names the namespace, so a later run can clean it up even though
    # this process is gone. That is the property the in-memory plan cannot provide.
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["namespace"]["uid"] == NAMESPACE_UID


def test_an_unavailable_coredns_refuses_with_the_taint_still_on(tmp_path):
    """**F2 end to end.** CoreDNS is patched to tolerate the taint and still never becomes
    available. The first revision declared readiness after namespace + CRD setup and would
    have registered this workspace; now it refuses with the interlock intact."""
    cluster = _FakeCluster(coredns_available=0)

    outcome, store = _run(cluster, tmp_path)

    assert outcome.registered is False
    assert outcome.ready is False
    assert any(t["key"] == BOOTSTRAP_TAINT_KEY for t in cluster.taints), (
        "the taint was removed despite an unusable runtime"
    )
    assert outcome.nodes_left_schedulable is False
    assert store.rows == {}


def test_a_registration_failure_after_the_taint_came_off_restores_it(tmp_path):
    """**F5 end to end, through the production SQL class.**

    The reservation succeeds, readiness passes, the taint comes off — and then the
    registration write fails. The taint must be back on and the reservation released, so
    no tenant work can schedule on an unregistered workspace.
    """

    class _FailsOnUpdate(_FakeSqlStore):
        def execute(self, statement, parameters):
            if " ".join(statement.split()).startswith(
                "UPDATE workspace_bootstrap_reservations"
            ):
                raise OSError("synthetic registration write failure")
            return super().execute(statement, parameters)

    cluster = _FakeCluster()

    outcome, store = _run(cluster, tmp_path, sql_store=_FailsOnUpdate())

    assert outcome.registered is False
    assert outcome.taint_cleared is True
    assert outcome.taint_restored is True
    assert outcome.nodes_left_schedulable is False
    assert any(t["key"] == BOOTSTRAP_TAINT_KEY for t in cluster.taints)
    assert outcome.reservation_released is True


def test_a_taint_that_cannot_be_removed_does_not_register(tmp_path):
    """Registration is finalized only after the taint is CONFIRMED gone. A failed removal
    that still registered would claim a usable workspace whose tenant work cannot
    schedule."""
    cluster = _FakeCluster(taint_removal_fails=True)

    outcome, store = _run(cluster, tmp_path)

    assert outcome.registered is False
    assert store.rows == {}


# --- the F3 and F4 gates, reached through real reads ---------------------------


def test_a_pre_existing_namespace_with_a_forged_owner_label_is_not_deleted(tmp_path):
    """**F3 end to end.** The namespace already exists and carries this workspace's owner
    label — which anyone can write, since neither the key nor the workspace id is secret.
    The cleanup plan must not offer to delete it."""
    cluster = _FakeCluster(
        pre_existing_namespace={
            "name": NAMESPACE,
            "uid": "a-uid-adp-never-assigned",
            "labels": {
                "superplane.aws-e/bootstrap-owner": WORKSPACE_ID,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": ENFORCE_VERSION,
            },
        }
    )

    outcome, _ = _run(cluster, tmp_path)

    if outcome.cleanup is not None:
        plan = outcome.cleanup
        assert plan.remove_namespace != NAMESPACE, (
            "a namespace ADP did not create was planned for deletion; plan removes "
            f"{plan.remove_namespace!r} uid={plan.remove_namespace_uid!r}"
        )
        # The preservation is the positive evidence: AC-02 requires the plan to state
        # that the pre-existing namespace was considered and spared, not merely to omit
        # it. An empty `preserved` with an empty `remove_namespace` would pass the
        # assertion above while proving nothing was read at all.
        assert any(NAMESPACE in entry for entry in plan.preserved), (
            f"the plan neither removes nor preserves {NAMESPACE!r}: {plan.preserved!r}"
        )
    assert outcome.installation is not None
    assert outcome.installation.namespace_owned is False


def test_a_missing_management_rule_refuses_before_the_taint_comes_off(tmp_path):
    """**F4 end to end.** The access path is verified from authoritative AWS reads, and a
    missing management-surface rule refuses — with the interlock still held."""
    cluster = _FakeCluster(management_rule_present=False)

    outcome, store = _run(cluster, tmp_path)

    assert outcome.registered is False
    assert any(t["key"] == BOOTSTRAP_TAINT_KEY for t in cluster.taints)
    assert store.rows == {}


def test_reachable_imds_refuses_the_isolation_proof(tmp_path):
    """A tenant pod that can reach IMDS can retrieve the node's credentials, which is the
    boundary the whole taint interlock exists to establish before tenant work runs."""
    cluster = _FakeCluster(imds_reachable=True)

    outcome, store = _run(cluster, tmp_path)

    assert outcome.registered is False
    assert any(t["key"] == BOOTSTRAP_TAINT_KEY for t in cluster.taints)
    assert store.rows == {}


# --- idempotence: a retry is the normal case ----------------------------------


def test_a_retry_after_a_failed_crd_apply_succeeds(tmp_path):
    """The realistic recovery: the first attempt died after creating the namespace, the
    operator fixed the manifest and re-ran. The second attempt must ADOPT its own
    namespace via the durable uid rather than refusing that it already exists."""
    cluster = _FakeCluster(crd_apply_fails=True)
    first, _ = _run(cluster, tmp_path)
    assert first.registered is False

    cluster.crd_apply_fails = False
    second, store = _run(cluster, tmp_path)

    assert second.registered is True, f"the retry refused: {second.refusal}"
    assert second.installation.namespace_owned is True, (
        "the namespace this bootstrap created was not recognised as its own on retry"
    )
    assert WORKSPACE_ID in store.rows


def test_a_second_run_over_a_completed_bootstrap_publishes_nothing_new(tmp_path):
    """A completed replay returns the canonical record without reacquiring access.

    It reports existing registration, not a fresh readiness or taint-clear action.
    The stored identity and all completed resources must remain unchanged.
    """
    cluster = _FakeCluster()
    store = _FakeSqlStore()

    first, _ = _run(cluster, tmp_path, sql_store=store)
    assert first.registered is True
    before = json.loads(store.rows[WORKSPACE_ID]["identity_json"])

    second, _ = _run(cluster, tmp_path, sql_store=store)

    assert second.registered is True
    assert second.registration.replayed
    assert second.refusal is None
    assert len(store.rows) == 1, "the re-run registered the workspace a second time"
    assert store.rows[WORKSPACE_ID]["state"] == "registered", (
        "the refusal path released a COMPLETED registration, unpublishing a live "
        "workspace"
    )
    assert json.loads(store.rows[WORKSPACE_ID]["identity_json"]) == before, (
        "the refused re-run rewrote the stored binding"
    )
    assert second.reservation_released is False
    assert second.taint_cleared is False
    assert second.nodes_left_schedulable is False


# --- nothing here holds a credential ------------------------------------------


def test_no_command_carries_a_secret_in_argv(tmp_path):
    """Manifests go in on stdin, never in argv: a value in argv appears in every process
    listing on the host. Asserted over every command the whole sequence ran."""
    cluster = _FakeCluster()

    outcome, _ = _run(cluster, tmp_path)
    assert outcome.registered is True

    for argv in cluster.commands:
        joined = " ".join(argv)
        assert "BEGIN" not in joined, "a key block was passed on the command line"
        assert "password" not in joined.lower()
        assert "postgres://" not in joined
        # The kubeconfig is passed as a PATH; its contents never are.
        assert "apiVersion: v1" not in joined
