"""Verify the scheduled CLI's AWS ownership and manager-loss contract offline."""

import datetime
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reconcile_instances", ROOT / "scripts/reconcile_instances.py")
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
NOW = datetime.datetime(2026, 11, 1, 19, 0, tzinfo=datetime.timezone.utc)
NAME = "adp-test-gitlab-job-42"
INSTANCE = "i-0123456789abcdef0"


def template_id(group_id):
    return f"lt-{int(group_id):017x}"


def security_group(group_id):
    return f"sg-{int(group_id):017x}"


def group(instances=(), desired=0, group_id="42"):
    return {"AutoScalingGroupName": NAME, "MinSize": 0, "MaxSize": 2,
            "DesiredCapacity": desired,
            "LaunchTemplate": {"LaunchTemplateId": template_id(group_id), "Version": "1"},
            "VPCZoneIdentifier": "subnet-0123456789abcdef0",
            "Tags": [{"Key": key, "Value": value} for key, value in
                     (("Component", "gitlab-job"), ("Environment", "test"), ("GroupId", group_id),
                      ("LaunchTemplateId", template_id(group_id)))],
            "Instances": [{"InstanceId": identifier, "ProtectedFromScaleIn": True} for identifier in instances]}


def ec2_instance(group_id="42", group_name=NAME, state="running"):
    return {"InstanceId": INSTANCE, "State": {"Name": state}, "ImageId": "ami-0123456789abcdef0",
            "SubnetId": "subnet-0123456789abcdef0",
            "SecurityGroups": [{"GroupId": security_group(group_id)}],
            "Tags": [{"Key": key, "Value": value} for key, value in
                     (("Component", "gitlab-job"), ("Environment", "test"), ("GroupId", group_id),
                      ("LaunchTemplateId", template_id(group_id)),
                      ("aws:autoscaling:groupName", group_name))]}


def page(*instances):
    return {"Reservations": [{"Instances": list(instances)}]}


class ReconcileTests(unittest.TestCase):
    def clients(self, *, stale=True, attached=True, disappears=True):
        autoscaling = Mock()
        ec2 = Mock()
        ssm = Mock()
        self.template_client(ec2)
        present = group((INSTANCE,) if attached else (), 1 if attached else 0)
        absent = group()
        autoscaling.describe_auto_scaling_groups.side_effect = (
            [{"AutoScalingGroups": [present]},
             *([{"AutoScalingGroups": [present]}] if not attached else []),
             {"AutoScalingGroups": [absent if disappears else present]}] if stale or not attached else
            [{"AutoScalingGroups": [present]}])
        ec2.describe_instances.side_effect = ([page(ec2_instance()), page() if disappears else page(ec2_instance())]
                                              if stale or not attached else [page(ec2_instance())])
        ssm.get_parameter.return_value = {"Parameter": {
            "Value": "active", "LastModifiedDate": NOW - datetime.timedelta(seconds=121 if stale else 30)}}
        return autoscaling, ec2, ssm

    def template_client(self, ec2):
        def describe(**query):
            group_id = str(int(query["LaunchTemplateId"].split("-", 1)[1], 16))
            return {"LaunchTemplateVersions": [{
                "LaunchTemplateId": query["LaunchTemplateId"], "VersionNumber": 1,
                "LaunchTemplateData": {"ImageId": "ami-0123456789abcdef0",
                                       "NetworkInterfaces": [{"Groups": [security_group(group_id)]}]}}]}

        ec2.describe_launch_template_versions.side_effect = describe

    def test_manager_loss_drains_group_without_deleting_manager(self):
        autoscaling, ec2, ssm = self.clients()
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        autoscaling.set_instance_protection.assert_called_once_with(
            AutoScalingGroupName=NAME, InstanceIds=[INSTANCE], ProtectedFromScaleIn=False)
        autoscaling.set_desired_capacity.assert_called_once_with(
            AutoScalingGroupName=NAME, DesiredCapacity=0, HonorCooldown=False)
        ec2.terminate_instances.assert_not_called()
        ssm.get_parameter.assert_called_once_with(
            Name="/adp/test/gitlab-runners/42/manager-alive", WithDecryption=False)

    def test_cli_timestamp_uses_server_modified_time_not_parameter_value(self):
        ssm = Mock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "active", "LastModifiedDate": NOW.isoformat()}}
        self.assertTrue(cleanup.manager_is_alive(ssm, "test", "42", NOW))
        ssm.get_parameter.return_value["Parameter"]["LastModifiedDate"] = (
            NOW - datetime.timedelta(seconds=121)).isoformat()
        self.assertFalse(cleanup.manager_is_alive(ssm, "test", "42", NOW))

    def test_healthy_manager_and_attached_instance_are_retained(self):
        autoscaling, ec2, ssm = self.clients(stale=False)
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
        autoscaling.set_instance_protection.assert_not_called()
        autoscaling.set_desired_capacity.assert_not_called()
        ec2.terminate_instances.assert_not_called()

    def test_owned_detached_orphan_is_terminated_after_manager_loss(self):
        autoscaling, ec2, ssm = self.clients(stale=True, attached=False)
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
        autoscaling.set_desired_capacity.assert_not_called()

    def test_detached_job_without_auto_scaling_tag_is_drained(self):
        autoscaling, ec2, ssm = self.clients(attached=False)
        detached = ec2_instance()
        detached["Tags"] = [tag for tag in detached["Tags"] if tag["Key"] != "aws:autoscaling:groupName"]
        ec2.describe_instances.side_effect = [page(detached), page()]
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])

    def test_detached_job_with_wrong_provenance_refuses_writes(self):
        for change, value in (("ImageId", "ami-0123456789abcdef1"),
                              ("SubnetId", "subnet-0123456789abcdef1"),
                              ("SecurityGroups", [{"GroupId": "sg-0123456789abcdef1"}])):
            with self.subTest(change=change):
                autoscaling, ec2, ssm = self.clients(attached=False)
                detached = ec2_instance()
                detached["Tags"] = [tag for tag in detached["Tags"] if tag["Key"] != "aws:autoscaling:groupName"]
                detached[change] = value
                ec2.describe_instances.side_effect = [page(detached)]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_missing_or_conflicting_persistent_owner_refuses_writes(self):
        for change in ("missing", "wrong", "duplicate"):
            with self.subTest(change=change):
                autoscaling, ec2, ssm = self.clients(attached=False)
                detached = ec2_instance()
                if change == "missing":
                    detached["Tags"] = [tag for tag in detached["Tags"] if tag["Key"] != "LaunchTemplateId"]
                elif change == "wrong":
                    next(tag for tag in detached["Tags"] if tag["Key"] == "LaunchTemplateId")["Value"] = template_id("43")
                else:
                    detached["Tags"].append({"Key": "LaunchTemplateId", "Value": template_id("43")})
                ec2.describe_instances.side_effect = [page(detached)]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_group_provenance_and_launch_read_are_required(self):
        for change in ("missing-tag", "wrong-tag", "wrong-template", "missing-template-version"):
            with self.subTest(change=change):
                autoscaling, ec2, ssm = self.clients(attached=False)
                owned_group = group()
                if change == "missing-tag":
                    owned_group["Tags"] = [tag for tag in owned_group["Tags"] if tag["Key"] != "LaunchTemplateId"]
                elif change == "wrong-tag":
                    next(tag for tag in owned_group["Tags"] if tag["Key"] == "LaunchTemplateId")["Value"] = template_id("43")
                elif change == "wrong-template":
                    owned_group["LaunchTemplate"]["LaunchTemplateId"] = template_id("43")
                else:
                    ec2.describe_launch_template_versions.return_value = {"LaunchTemplateVersions": []}
                    ec2.describe_launch_template_versions.side_effect = None
                autoscaling.describe_auto_scaling_groups.side_effect = [{"AutoScalingGroups": [owned_group]}]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_recovered_manager_still_cleans_detached_job(self):
        autoscaling, ec2, ssm = self.clients(stale=False, attached=False)
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
        self.assertEqual(ec2.describe_instances.call_count, 2)
        autoscaling.set_instance_protection.assert_not_called()
        autoscaling.set_desired_capacity.assert_not_called()

    def test_recovered_manager_keeps_attached_jobs_while_cleaning_detached_job(self):
        autoscaling, ec2, ssm = self.clients(stale=False)
        attached = ec2_instance()
        attached["InstanceId"] = "i-0123456789abcdef1"
        current = group((attached["InstanceId"],), 1)
        autoscaling.describe_auto_scaling_groups.side_effect = None
        autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [current]}
        ec2.describe_instances.side_effect = [page(attached, ec2_instance()), page(attached)]
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
        self.assertEqual(ec2.describe_instances.call_count, 2)
        autoscaling.set_instance_protection.assert_not_called()
        autoscaling.set_desired_capacity.assert_not_called()

    def test_recovered_manager_cannot_hide_remaining_detached_job(self):
        autoscaling, ec2, ssm = self.clients(stale=False, attached=False, disappears=False)
        ticks = iter((0, 361))
        with self.assertRaises(cleanup.ReconcileError):
            cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                              clock=lambda: next(ticks), pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
        autoscaling.set_instance_protection.assert_not_called()
        autoscaling.set_desired_capacity.assert_not_called()

    def test_scale_up_between_asg_and_ec2_reads_is_not_terminated(self):
        autoscaling, ec2, ssm = self.clients(stale=False, attached=False)
        events = []

        def describe_group(**query):
            snapshot = group(desired=1) if not events else group((INSTANCE,), 1)
            events.append("asg")
            return {"AutoScalingGroups": [snapshot]}

        def describe_instances(**query):
            events.append("ec2")
            return page(ec2_instance())

        autoscaling.describe_auto_scaling_groups.side_effect = describe_group
        ec2.describe_instances.side_effect = describe_instances
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        self.assertEqual(events[:3], ["asg", "ec2", "asg"])
        self.assert_no_mutation(autoscaling, ec2)

    def test_scale_up_preserves_new_member_and_terminates_only_confirmed_orphan(self):
        autoscaling, ec2, ssm = self.clients(stale=False, attached=False)
        attached = group((INSTANCE,), 1)
        autoscaling.describe_auto_scaling_groups.side_effect = [
            {"AutoScalingGroups": [group(desired=1)]},
            {"AutoScalingGroups": [attached]},
            {"AutoScalingGroups": [attached]}]
        orphan = ec2_instance()
        orphan["InstanceId"] = "i-0123456789abcdef1"
        ec2.describe_instances.side_effect = [page(ec2_instance(), orphan), page(ec2_instance())]

        def terminate(**query):
            self.assertEqual(autoscaling.describe_auto_scaling_groups.call_count, 2)
            self.assertEqual(query["InstanceIds"], [orphan["InstanceId"]])

        ec2.terminate_instances.side_effect = terminate
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[orphan["InstanceId"]])
        autoscaling.set_instance_protection.assert_not_called()
        autoscaling.set_desired_capacity.assert_not_called()

    def test_ambiguous_launch_retries_until_attached_or_confirmed_detached(self):
        for resolution in ("attached", "detached", "unresolved"):
            with self.subTest(resolution=resolution):
                autoscaling, ec2, ssm = self.clients(stale=False, attached=False)
                reads = [0]
                elapsed = [0]

                def describe_group(**query):
                    reads[0] += 1
                    snapshot = group(desired=1)
                    if reads[0] >= 3 and resolution == "attached":
                        snapshot = group((INSTANCE,), 1)
                    elif reads[0] >= 3 and resolution == "detached":
                        snapshot = group()
                    return {"AutoScalingGroups": [snapshot]}

                def pause(seconds):
                    elapsed[0] += seconds

                def terminate(**query):
                    self.assertGreaterEqual(reads[0], 4)

                autoscaling.describe_auto_scaling_groups.side_effect = describe_group
                ec2.describe_instances.side_effect = lambda **query: (
                    page() if ec2.terminate_instances.called else page(ec2_instance()))
                ec2.terminate_instances.side_effect = terminate
                if resolution == "unresolved":
                    with self.assertRaises(cleanup.ReconcileError):
                        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                                          clock=lambda: elapsed[0], pause=pause)
                    self.assertEqual(elapsed[0], 360)
                else:
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                                      clock=lambda: elapsed[0], pause=pause)
                if resolution == "detached":
                    ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
                else:
                    ec2.terminate_instances.assert_not_called()
                autoscaling.set_instance_protection.assert_not_called()
                autoscaling.set_desired_capacity.assert_not_called()

    def test_healthy_replacement_defers_termination_until_membership_stabilizes(self):
        autoscaling, ec2, ssm = self.clients(stale=False, attached=False)
        retiring_id = "i-0123456789abcdef1"
        retiring = group((retiring_id,), 1)
        retiring["Instances"][0]["LifecycleState"] = "Terminating"
        attached = group((INSTANCE,), 1)
        autoscaling.describe_auto_scaling_groups.side_effect = [
            {"AutoScalingGroups": [retiring]},
            {"AutoScalingGroups": [retiring]},
            {"AutoScalingGroups": [attached]}]
        terminated = ec2_instance(state="terminated")
        terminated["InstanceId"] = retiring_id
        ec2.describe_instances.side_effect = [page(terminated, ec2_instance()), page(ec2_instance())]
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        self.assert_no_mutation(autoscaling, ec2)

    def test_changed_group_on_membership_refresh_refuses_termination(self):
        for change in ("owner", "template", "subnets", "capacity"):
            with self.subTest(change=change):
                autoscaling, ec2, ssm = self.clients(stale=False, attached=False)
                refreshed = group()
                if change == "owner":
                    next(tag for tag in refreshed["Tags"] if tag["Key"] == "GroupId")["Value"] = "43"
                elif change == "template":
                    refreshed["LaunchTemplate"]["Version"] = "2"
                elif change == "subnets":
                    refreshed["VPCZoneIdentifier"] = "subnet-0123456789abcdef1"
                else:
                    refreshed["MaxSize"] = 3
                autoscaling.describe_auto_scaling_groups.side_effect = [
                    {"AutoScalingGroups": [group()]}, {"AutoScalingGroups": [refreshed]}]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_terminated_member_waits_for_asg_removal_while_other_group_drains(self):
        autoscaling, ec2, ssm = Mock(), Mock(), Mock()
        self.template_client(ec2)
        reads = {"42": 0, "43": 0}
        other_instance = "i-0123456789abcdef1"
        events = []

        def describe_group(**query):
            name = query["AutoScalingGroupNames"][0]
            group_id = name.rsplit("-", 1)[-1]
            reads[group_id] += 1
            snapshot = group(group_id=group_id)
            if group_id == "42" and reads[group_id] < 3:
                snapshot = group((INSTANCE,), 0)
                snapshot["Instances"][0].update(ProtectedFromScaleIn=False, LifecycleState="Terminating")
            elif group_id == "43" and reads[group_id] == 1:
                snapshot = group((other_instance,), 1, group_id)
            snapshot["AutoScalingGroupName"] = name
            return {"AutoScalingGroups": [snapshot]}

        def describe_instances(**query):
            filters = {entry["Name"]: entry["Values"] for entry in query["Filters"]}
            if filters["tag:GroupId"] == ["42"]:
                retired = ec2_instance(state="terminated")
                retired["ImageId"] = "ami-0123456789abcdef1"
                retired["SecurityGroups"] = []
                if "instance-state-name" in filters and "terminated" not in filters["instance-state-name"]:
                    return page()
                return page(retired)
            if reads["43"] == 1:
                active = ec2_instance(group_id="43", group_name="adp-test-gitlab-job-43")
                active["InstanceId"] = other_instance
                return page(active)
            return page()

        autoscaling.describe_auto_scaling_groups.side_effect = describe_group
        ec2.describe_instances.side_effect = describe_instances
        ssm.get_parameter.return_value = {"Parameter": {
            "Value": "active", "LastModifiedDate": NOW - datetime.timedelta(seconds=121)}}
        autoscaling.set_desired_capacity.side_effect = lambda **query: events.append("drain-43")
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42", "43"], now=NOW,
                          clock=lambda: 0, pause=lambda _: events.append("wait"))
        self.assertEqual(events, ["drain-43", "wait"])
        autoscaling.set_desired_capacity.assert_called_once_with(
            AutoScalingGroupName="adp-test-gitlab-job-43", DesiredCapacity=0, HonorCooldown=False)
        autoscaling.set_instance_protection.assert_called_once_with(
            AutoScalingGroupName="adp-test-gitlab-job-43", InstanceIds=[other_instance], ProtectedFromScaleIn=False)
        ec2.terminate_instances.assert_not_called()
        self.assertEqual(reads["42"], 3)

    def test_terminated_previous_release_does_not_block_current_orphan_cleanup(self):
        autoscaling, ec2, ssm = self.clients(attached=False)
        retired = ec2_instance(state="terminated")
        retired["InstanceId"] = "i-0123456789abcdef1"
        retired["ImageId"] = "ami-0123456789abcdef1"
        retired["SecurityGroups"] = [{"GroupId": "sg-0123456789abcdef1"}]
        ec2.describe_instances.side_effect = [page(retired, ec2_instance()), page(retired)]
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
        self.assertEqual(ec2.describe_instances.call_count, 2)

    def test_nonterminated_previous_release_still_refuses_mutation(self):
        for state in ("pending", "running", "shutting-down", "stopping", "stopped"):
            with self.subTest(state=state):
                autoscaling, ec2, ssm = self.clients(attached=False)
                unproven = ec2_instance(state=state)
                unproven["ImageId"] = "ami-0123456789abcdef1"
                ec2.describe_instances.side_effect = [page(unproven)]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_remaining_instance_fails_inventory_verification(self):
        autoscaling, ec2, ssm = self.clients(disappears=False)
        ticks = iter((0, 361))
        with self.assertRaises(cleanup.ReconcileError):
            cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                              clock=lambda: next(ticks), pause=lambda _: None)
        autoscaling.set_desired_capacity.assert_called_once()

    def test_wrong_group_tags_refuse_mutation(self):
        autoscaling, ec2, ssm = self.clients()
        autoscaling.describe_auto_scaling_groups.side_effect = None
        autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [group(group_id="43")]}
        with self.assertRaises(cleanup.ReconcileError):
            cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
        autoscaling.set_desired_capacity.assert_not_called()
        ec2.terminate_instances.assert_not_called()

    def test_scheduled_cli_uses_only_dedicated_role_and_mocked_transport(self):
        autoscaling, ec2, ssm = self.clients(stale=False)
        ssm.get_parameter.return_value["Parameter"]["LastModifiedDate"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30))
        identity = Mock()
        identity.get_caller_identity.return_value = {
            "Account": "123456789012", "Arn":
            "arn:aws:sts::123456789012:assumed-role/adp-test-gitlab-runners-reconciler/fixture"}
        clients = {"sts": identity, "autoscaling": autoscaling, "ec2": ec2, "ssm": ssm}
        output = io.StringIO()
        with patch.object(cleanup, "AwsCli", side_effect=lambda service, region: clients[service]) as factory, \
             patch.object(sys, "argv", ["reconcile_instances.py", "--environment", "test",
                                        "--region", "us-east-1", "--group-ids", "42"]), \
             patch.object(sys, "stdout", output):
            cleanup.main()
        self.assertEqual(output.getvalue(), "runner_reconciliation_verified\n")
        factory.assert_any_call("ssm", "us-east-1")

    def test_cli_transport_uses_existing_aws_binary_and_scoped_region(self):
        transport = Mock(return_value=Mock(returncode=0, stdout=b'{"AutoScalingGroups": []}'))
        response = cleanup.AwsCli("autoscaling", "us-east-1", run=transport).describe_auto_scaling_groups(
            AutoScalingGroupNames=[NAME])
        self.assertEqual(response, {"AutoScalingGroups": []})
        self.assertEqual(transport.call_args.args[0][:6], [
            "aws", "autoscaling", "describe-auto-scaling-groups", "--region", "us-east-1", "--cli-input-json"])
        self.assertIn(NAME, transport.call_args.args[0][6])

    def test_cli_transport_accepts_empty_success_only_for_known_writes(self):
        for service, operation in (("ec2", "terminate_instances"),
                                   ("autoscaling", "set_instance_protection"),
                                   ("autoscaling", "set_desired_capacity")):
            for output in (b"", b" \n"):
                with self.subTest(service=service, operation=operation, output=output):
                    transport = Mock(return_value=Mock(returncode=0, stdout=output))
                    client = cleanup.AwsCli(service, "us-east-1", run=transport)
                    self.assertEqual(getattr(client, operation)(InstanceIds=[INSTANCE]), {})
                    self.assertIn(operation.replace("_", "-"), transport.call_args.args[0])
                    transport.return_value.returncode = 1
                    with self.assertRaises(cleanup.ReconcileError):
                        getattr(client, operation)(InstanceIds=[INSTANCE])
        for output in (b"", b" \n", b"not json"):
            with self.subTest(read_output=output):
                transport = Mock(return_value=Mock(returncode=0, stdout=output))
                with self.assertRaises((cleanup.ReconcileError, ValueError)):
                    cleanup.AwsCli("ec2", "us-east-1", run=transport).describe_instances()

    def test_both_stale_groups_start_draining_before_either_is_polled(self):
        autoscaling, ec2, ssm = Mock(), Mock(), Mock()
        self.template_client(ec2)
        identifiers = {"42": INSTANCE, "43": "i-0123456789abcdef1"}
        reads = {group_id: 0 for group_id in identifiers}
        events = []
        current_time = [0]

        def describe_group(**query):
            group_id = query["AutoScalingGroupNames"][0].rsplit("-", 1)[-1]
            reads[group_id] += 1
            if reads[group_id] > 1:
                events.append(("poll", group_id))
            snapshot = group((identifiers[group_id],), 1, group_id) if reads[group_id] < 3 else group(group_id=group_id)
            snapshot["AutoScalingGroupName"] = query["AutoScalingGroupNames"][0]
            return {"AutoScalingGroups": [snapshot]}

        def describe_instances(**query):
            group_id = query["Filters"][2]["Values"][0]
            if reads[group_id] < 3:
                instance = ec2_instance(group_id=group_id, group_name=f"adp-test-gitlab-job-{group_id}")
                instance["InstanceId"] = identifiers[group_id]
                return page(instance)
            return page()

        autoscaling.describe_auto_scaling_groups.side_effect = describe_group
        ec2.describe_instances.side_effect = describe_instances
        ssm.get_parameter.return_value = {"Parameter": {
            "Value": "active", "LastModifiedDate": NOW - datetime.timedelta(seconds=121)}}
        autoscaling.set_instance_protection.side_effect = (
            lambda **query: events.append(("unprotect", query["AutoScalingGroupName"])))
        autoscaling.set_desired_capacity.side_effect = (
            lambda **query: events.append(("drain", query["AutoScalingGroupName"])))

        def pause(seconds):
            events.append(("wait", seconds))
            current_time[0] += seconds

        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42", "43"], now=NOW,
                          clock=lambda: current_time[0], pause=pause)
        self.assertEqual(events[:4], [
            ("unprotect", "adp-test-gitlab-job-42"), ("drain", "adp-test-gitlab-job-42"),
            ("unprotect", "adp-test-gitlab-job-43"), ("drain", "adp-test-gitlab-job-43")])
        self.assertEqual([entry for entry in events if entry[0] == "poll"],
                         [("poll", "42"), ("poll", "43"), ("poll", "42"), ("poll", "43")])
        self.assertEqual([entry for entry in events if entry[0] == "wait"], [("wait", 5)])

    def test_two_stale_groups_share_one_deadline_and_fail_on_remaining_orphan(self):
        autoscaling, ec2, ssm = Mock(), Mock(), Mock()
        self.template_client(ec2)
        current_time = [0]
        identifiers = {"42": INSTANCE, "43": "i-0123456789abcdef1"}

        def describe_group(**query):
            name = query["AutoScalingGroupNames"][0]
            group_id = name.rsplit("-", 1)[-1]
            snapshot = group((identifiers[group_id],), 1, group_id)
            snapshot["AutoScalingGroupName"] = name
            return {"AutoScalingGroups": [snapshot]}

        def describe_instances(**query):
            group_id = query["Filters"][2]["Values"][0]
            instance = ec2_instance(group_id=group_id, group_name=f"adp-test-gitlab-job-{group_id}")
            instance["InstanceId"] = identifiers[group_id]
            return page(instance)

        autoscaling.describe_auto_scaling_groups.side_effect = describe_group
        ec2.describe_instances.side_effect = describe_instances
        ssm.get_parameter.return_value = {"Parameter": {
            "Value": "active", "LastModifiedDate": NOW - datetime.timedelta(seconds=121)}}

        def pause(seconds):
            current_time[0] += seconds

        with self.assertRaises(cleanup.ReconcileError):
            cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42", "43"], now=NOW,
                              clock=lambda: current_time[0], pause=pause)
        self.assertEqual(current_time[0], 360)
        self.assertEqual(autoscaling.set_desired_capacity.call_count, 2)

    def test_group_list_is_strictly_scoped(self):
        for raw in ("42,42", "42,", "042", "-1", "42,43,44,45,46", "42;43"):
            with self.subTest(raw=raw), self.assertRaises(cleanup.ReconcileError):
                cleanup.group_ids(raw)
        self.assertEqual(cleanup.group_ids("42,43"), ["42", "43"])

    def assert_no_mutation(self, autoscaling, ec2):
        autoscaling.set_instance_protection.assert_not_called()
        autoscaling.set_desired_capacity.assert_not_called()
        ec2.terminate_instances.assert_not_called()

    def test_unrelated_instance_is_never_terminated(self):
        for key, value in (("GroupId", "43"), ("Environment", "production"),
                           ("aws:autoscaling:groupName", "unrelated")):
            with self.subTest(key=key):
                autoscaling, ec2, ssm = self.clients(attached=False)
                foreign = ec2_instance()
                next(tag for tag in foreign["Tags"] if tag["Key"] == key)["Value"] = value
                ec2.describe_instances.side_effect = [page(foreign)]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_invalid_later_group_refuses_earlier_group_drain(self):
        autoscaling, ec2, ssm = self.clients()
        other = group(group_id="43")
        other["AutoScalingGroupName"] = "adp-test-gitlab-job-43"
        autoscaling.describe_auto_scaling_groups.side_effect = [
            {"AutoScalingGroups": [group((INSTANCE,), 1)]},
            {"AutoScalingGroups": [other]}]
        ec2.describe_instances.side_effect = [page(ec2_instance()), page(ec2_instance(group_id="42"))]
        with self.assertRaises(cleanup.ReconcileError):
            cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42", "43"], now=NOW)
        self.assert_no_mutation(autoscaling, ec2)

    def test_later_detached_group_with_wrong_subnet_blocks_first_drain(self):
        autoscaling, ec2, ssm = self.clients(attached=False)
        later_group = group(group_id="43")
        later_group["AutoScalingGroupName"] = "adp-test-gitlab-job-43"
        autoscaling.describe_auto_scaling_groups.side_effect = [
            {"AutoScalingGroups": [group()]}, {"AutoScalingGroups": [later_group]}]
        first = ec2_instance()
        later = ec2_instance(group_id="43", group_name="adp-test-gitlab-job-43")
        later["Tags"] = [tag for tag in later["Tags"] if tag["Key"] != "aws:autoscaling:groupName"]
        later["SubnetId"] = "subnet-0123456789abcdef1"
        ec2.describe_instances.side_effect = [page(first), page(later)]
        with self.assertRaises(cleanup.ReconcileError):
            cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42", "43"], now=NOW)
        self.assert_no_mutation(autoscaling, ec2)

    def test_ambiguous_asg_and_ec2_owner_tags_refuse_mutation(self):
        for resource in ("asg", "ec2"):
            with self.subTest(resource=resource):
                autoscaling, ec2, ssm = self.clients()
                if resource == "asg":
                    ambiguous = group((INSTANCE,), 1)
                    ambiguous["Tags"].insert(0, {"Key": "GroupId", "Value": "43"})
                    autoscaling.describe_auto_scaling_groups.side_effect = [
                        {"AutoScalingGroups": [ambiguous]}]
                else:
                    ambiguous = ec2_instance()
                    ambiguous["Tags"].insert(0, {"Key": "aws:autoscaling:groupName", "Value": "unrelated"})
                    ec2.describe_instances.side_effect = [page(ambiguous)]
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_missing_member_or_duplicate_instance_pages_refuse_mutation(self):
        for pages in ([page()],
                      [{"Reservations": page(ec2_instance())["Reservations"], "NextToken": "next"},
                       page(ec2_instance())],
                      [{"Reservations": [], "NextToken": "next"},
                       {"Reservations": [], "NextToken": "next"}]):
            with self.subTest(pages=pages):
                autoscaling, ec2, ssm = self.clients()
                ec2.describe_instances.side_effect = pages
                with self.assertRaises(cleanup.ReconcileError):
                    cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW)
                self.assert_no_mutation(autoscaling, ec2)

    def test_detached_owned_instance_on_later_page_is_drained(self):
        autoscaling, ec2, ssm = self.clients(attached=False)
        ec2.describe_instances.side_effect = [
            {"Reservations": [], "NextToken": "next"}, page(ec2_instance()), page()]
        cleanup.reconcile(autoscaling, ec2, ssm, "test", ["42"], now=NOW,
                          clock=lambda: 0, pause=lambda _: None)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=[INSTANCE])
        self.assertEqual(ec2.describe_instances.call_args_list[1].kwargs["NextToken"], "next")


if __name__ == "__main__":
    unittest.main()
