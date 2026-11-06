"""A rollout must bind a promoted version, its own namespace and fresh identity."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import activate_manager as activation
from deployment_guard import check_plan

OPERATION = "00000000-0000-4000-8000-000000000001"
SOURCE = "a" * 40


class ActivationTests(unittest.TestCase):
    def terminating_fixture(self, change=None):
        ns = "gitlab-runner-42"
        pod = {"metadata": {"name": "gitlab-runner-abc-def", "namespace": ns, "uid": "pod-uid", "resourceVersion": "7",
                "deletionTimestamp": "2026-01-01T00:00:00Z", "ownerReferences": [
                    {"kind": "ReplicaSet", "name": "gitlab-runner-abc", "uid": "rs-uid", "controller": True}]},
               "spec": {"serviceAccountName": "gitlab-runner"}}
        rs = {"metadata": {"namespace": ns, "uid": "rs-uid", "ownerReferences": [
            {"kind": "Deployment", "name": "gitlab-runner", "uid": "deployment-uid", "controller": True}]}}
        deployment = {"metadata": {"namespace": ns, "uid": "deployment-uid"}, "spec": {"strategy": {"type": "Recreate"}, "replicas": 1}}
        if change == "active":
            del pod["metadata"]["deletionTimestamp"]
        elif change == "finalizer":
            pod["metadata"]["finalizers"] = ["retain"]
        elif change == "foreign":
            rs["metadata"]["ownerReferences"][0]["uid"] = "foreign-deployment"
        elif change == "account":
            pod["spec"]["serviceAccountName"] = "foreign"
        mutations = []

        def kube(*args):
            if args[:2] == ("get", "pods"):
                return {"items": [pod]}
            if args[:2] == ("get", "serviceaccount"):
                return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/adp-dev-gitlab-runner-42"}}}
            if args[:2] == ("get", "replicaset"):
                return rs
            if args[:2] == ("get", "deployment"):
                return deployment
            if args[0] == "delete":
                mutations.append((args[2], json.loads(Path(args[-1]).read_text())))
            elif args[0] != "wait":
                raise AssertionError(args)
        return kube, mutations

    def test_only_owned_terminating_pod_receives_bounded_uid_conditional_delete(self):
        kube, mutations = self.terminating_fixture()
        activation.recover_terminating_manager(kube, "42", "123456789012")
        self.assertEqual(mutations, [("/api/v1/namespaces/gitlab-runner-42/pods/gitlab-runner-abc-def",
            {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 30,
             "preconditions": {"uid": "pod-uid", "resourceVersion": "7"}})])

    def test_active_foreign_or_finalized_manager_never_deleted(self):
        kube, mutations = self.terminating_fixture("active")
        activation.recover_terminating_manager(kube, "42", "123456789012")
        self.assertEqual(mutations, [])
        for change in ("finalizer", "foreign", "account"):
            with self.subTest(change=change):
                kube, mutations = self.terminating_fixture(change)
                with self.assertRaises(activation.ActivationError):
                    activation.recover_terminating_manager(kube, "42", "123456789012")
                self.assertEqual(mutations, [])

    def test_shutdown_race_requires_original_uid_disappearance(self):
        for failure_at in ("delete", "wait"):
            for remaining in ("gone", "replacement", "original"):
                with self.subTest(failure_at=failure_at, remaining=remaining):
                    base, mutations = self.terminating_fixture()
                    failed = False

                    def kube(*args):
                        nonlocal failed
                        if failed and args[:2] == ("get", "pods"):
                            return {"items": [] if remaining == "gone" else [{"metadata": {
                                "uid": "pod-uid" if remaining == "original" else "replacement-uid"}}]}
                        result = base(*args)
                        if args[0] == failure_at:
                            failed = True
                            raise ValueError("runner_command_failed")
                        return result

                    if remaining == "original":
                        with self.assertRaises(ValueError):
                            activation.recover_terminating_manager(kube, "42", "123456789012")
                    else:
                        activation.recover_terminating_manager(kube, "42", "123456789012")
                    self.assertEqual(len(mutations), 1)
                    self.assertEqual(mutations[0][1]["preconditions"]["uid"], "pod-uid")

    def test_group_not_in_reviewed_plan_cannot_activate(self):
        plan = {"planned_values": {"outputs": {"group_namespaces": {"value": {"42": {"manager": "gitlab-runner-42"}}}},
                "root_module": {"resources": [{"address": "terraform_data.discovery", "values": {"input": {"cluster_name": "test-cluster"}}}]}}}
        self.assertEqual(activation.selected_group(plan, "42"), "test-cluster")
        with self.assertRaises((activation.ActivationError, KeyError)):
            activation.selected_group(plan, "43")

    def test_staged_or_wrong_current_secret_refuses_without_secret_read(self):
        record = {"group_id": 42, "operation_id": OPERATION, "secret_version_id": OPERATION,
                  "phase": "promoted", "new_runner_id": 101}
        calls = []

        def call(service, operation, **kwargs):
            calls.append((service, operation))
            if service == "ssm":
                return {"Parameter": {"Value": json.dumps(record)}}
            return {"VersionIdsToStages": {OPERATION: ["AWSCURRENT"]}}

        self.assertEqual(activation.promoted_operation("42", OPERATION, call), 101)
        record["phase"] = "staged"
        with self.assertRaises(activation.ActivationError):
            activation.promoted_operation("42", OPERATION, call)
        self.assertNotIn(("secretsmanager", "get-secret-value"), calls)
        record["phase"] = "promoted"
        with self.assertRaises(activation.ActivationError):
            activation.promoted_operation("42", OPERATION, lambda service, operation, **kwargs:
                                          {"VersionIdsToStages": {"wrong": ["AWSCURRENT"]}} if service == "secretsmanager" else call(service, operation, **kwargs))

    def test_stale_or_different_runner_receipt_never_passes(self):
        now = datetime.now(timezone.utc)
        parameter = {"Value": json.dumps({"runner_id": 101, "system_id": "r_" + "a" * 12}), "LastModifiedDate": now}
        self.assertEqual(activation.fresh_registration(parameter, 101, now - timedelta(seconds=5), now)["runner_id"], 101)
        for changed in ({"LastModifiedDate": now - timedelta(seconds=10)},
                        {"LastModifiedDate": now + timedelta(seconds=10)},
                        {"Value": json.dumps({"runner_id": 102, "system_id": "r_" + "a" * 12})}):
            with self.subTest(changed=changed), self.assertRaises(activation.ActivationError):
                activation.fresh_registration({**parameter, **changed}, 101, now - timedelta(seconds=5), now)

    def rollout_fixture(self, *, wrong_role=False, ready=True):
        calls = []
        deployment = {"metadata": {"namespace": "gitlab-runner-42", "uid": "owned-deployment", "resourceVersion": "1", "generation": 1},
                      "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                               "template": {"metadata": {"annotations": {}}, "spec": {"serviceAccountName": "gitlab-runner"}}},
                      "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1,
                                 "readyReplicas": 1 if ready else 0, "availableReplicas": 1}}

        def kube(*args):
            calls.append(args)
            if args[:2] == ("get", "namespace"):
                return {"metadata": {"labels": {"adp-group-id": "42"}}}
            if args[:2] == ("get", "serviceaccount"):
                return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn":
                    "arn:aws:iam::123456789012:role/" + ("foreign" if wrong_role else "adp-dev-gitlab-runner-42")}}}
            if args[:2] == ("get", "deployment"):
                return copy.deepcopy(deployment)
            if args[0] == "patch":
                patch = json.loads(args[-1])
                self.assertEqual(patch["metadata"]["resourceVersion"], "1")
                deployment["spec"]["template"]["metadata"]["annotations"].update(patch["spec"]["template"]["metadata"]["annotations"])

        def ssm(*args, **kwargs):
            return {"Parameter": {"LastModifiedDate": datetime.now(timezone.utc),
                                  "Value": json.dumps({"runner_id": 101, "system_id": "r_" + "a" * 12})}}

        return kube, ssm, calls

    def test_rotation_rolls_once_and_reconciles_the_same_operation(self):
        plan = {
            "complete": True, "applyable": False, "errored": False,
            "configuration": {"root_module": {"resources": [
                {"type": resource_type} for resource_type in
                ("aws_autoscaling_group", "aws_launch_template", "helm_release")
            ]}},
            "resource_changes": [{"change": {"actions": ["no-op"]}}],
            "output_changes": {"group_namespaces": {"actions": ["no-op"]}},
        }
        check_plan(plan, "apply")
        kube, ssm, calls = self.rollout_fixture()
        for _ in range(2):
            result = activation.rollout(kube, "42", OPERATION, SOURCE, "123456789012", 101, call=ssm)
            self.assertEqual(result["runner_id"], 101)
        self.assertEqual(sum(args[0] == "patch" for args in calls), 1)
        self.assertTrue(all("delete" not in args for args in calls))

    def test_wrong_role_refuses_before_patch_and_unready_manager_never_qualifies(self):
        kube, ssm, calls = self.rollout_fixture(wrong_role=True)
        with self.assertRaises(activation.ActivationError):
            activation.rollout(kube, "42", OPERATION, SOURCE, "123456789012", 101, call=ssm)
        self.assertFalse(any(args[0] == "patch" for args in calls))
        kube, ssm, _ = self.rollout_fixture(ready=False)
        with self.assertRaises(activation.ActivationError):
            activation.rollout(kube, "42", OPERATION, SOURCE, "123456789012", 101, call=ssm)


if __name__ == "__main__":
    unittest.main()
