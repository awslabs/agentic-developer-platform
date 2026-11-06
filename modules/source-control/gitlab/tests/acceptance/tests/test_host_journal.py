import base64
import copy
import hashlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import host_observation as host
import host_journal as journal
import collect_host as collector

BOOT = "00000000-0000-0000-0000-000000000001"
CONTAINER = "a" * 64


class HostJournalTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.TemporaryFile()
        self.addCleanup(self.file.close)
        self.console = io.BytesIO()
        self.journal = journal.Journal(self.file, self.console, BOOT, clock=lambda: 123456789)
        self.event = {"Type": "container", "Action": "start", "timeNano": 123456,
                      "Actor": {"ID": CONTAINER, "Attributes": {
                          host.PREFIX + "managed": "true", host.PREFIX + "job.id": "11",
                          host.PREFIX + "project.id": "17", host.PREFIX + "pipeline.id": "23",
                          host.PREFIX + "job.sha": "b" * 40, host.PREFIX + "runner.id": "glrt-synthetic-canary",
                          "env": "glpat-synthetic-private-event-value"}}}

    def records(self):
        return collector.parse_console(self.console.getvalue().decode())

    def test_safe_events_survive_console_roundtrip_without_private_event_attributes(self):
        self.journal.emit("stream_started", {"docker_since_unix_seconds": 1})
        observation = {"container_id": CONTAINER, "job_id": 11, "kernel": {"uids": [10001] * 4}}
        observe = Mock(return_value=observation)
        journal.event_record(self.event, self.journal, observe=observe)
        rows, problems = collector.merge_records([], self.records())
        self.assertEqual(problems, [])
        self.assertEqual([row["sequence"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[2]["data"], observation)
        observe.assert_called_once_with(CONTAINER, {"job_id": 11, "project_id": 17,
            "pipeline_id": 23, "source_revision": "b" * 40})
        self.assertNotIn("glrt-", json.dumps(rows))
        self.assertNotIn("glpat-", json.dumps(rows))

    def test_exited_container_keeps_the_failed_attempt_without_fabricating_identity(self):
        self.journal.emit("stream_started", {})
        journal.event_record(self.event, self.journal, observe=Mock(side_effect=RuntimeError("glrt-private-exception")))
        rows, problems = collector.merge_records([], self.records())
        self.assertEqual(problems, ["incomplete_host_observation"])
        self.assertEqual(rows[-1]["kind"], "observation_incomplete")
        self.assertNotIn("glrt-", json.dumps(rows))
        self.assertNotIn("kernel", rows[-1]["data"])

    def test_unknown_unmanaged_event_is_ignored_and_bad_native_identity_refuses(self):
        event = copy.deepcopy(self.event)
        event["Actor"]["Attributes"][host.PREFIX + "managed"] = "false"
        observe = Mock()
        journal.event_record(event, self.journal, observe=observe)
        observe.assert_not_called()
        self.assertEqual(self.console.getvalue(), b"")
        for field in ("job.id", "project.id", "pipeline.id", "job.sha"):
            event = copy.deepcopy(self.event)
            event["Actor"]["Attributes"][host.PREFIX + field] = "unexpected"
            with self.subTest(field=field), self.assertRaises(host.ObservationError):
                journal.event_record(event, self.journal, observe=observe)

    def test_duplicate_snapshots_reconcile_but_truncation_and_conflicts_do_not_pass(self):
        self.journal.emit("stream_started", {})
        self.journal.emit("heartbeat", {})
        self.journal.emit("heartbeat", {})
        rows = self.records()
        self.assertEqual(collector.merge_records(rows[:2], rows[1:]), (rows, []))
        self.assertEqual(collector.merge_records([], rows[1:])[1], ["missing_history"])
        self.assertEqual(collector.merge_records([], [rows[0], rows[2]])[1], ["missing_history"])
        conflict = copy.deepcopy(rows[1])
        conflict["data"] = {"changed": True}
        with self.assertRaisesRegex(host.ObservationError, "conflict_or_restart"):
            collector.merge_records(rows, [conflict])

    def test_restart_uses_separate_stream_and_rejects_unobserved_gap(self):
        first = self.journal.emit("stream_started", {"executor_ready": False})
        second_console = io.BytesIO()
        second = journal.Journal(self.file, second_console, BOOT, clock=lambda: 123456789 + 30 * 10**9)
        restarted = second.emit("stream_started", {"executor_ready": True})
        self.assertNotEqual(first["stream_id"], restarted["stream_id"])
        records = self.records() + collector.parse_console(second_console.getvalue().decode())
        self.assertEqual(collector.merge_records([], records), (records, []))
        late = copy.deepcopy(restarted)
        late["observed_at_unix_ns"] += 61 * 10**9
        self.assertIn("restart_observation_gap", collector.merge_records([], [first, late])[1])

    def test_observer_can_read_host_netlink_without_network_sockets_or_namespace(self):
        unit = (ROOT.parents[1] / "runners/image/adp-job-observer.service").read_text()
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_NETLINK", unit)
        self.assertNotIn("PrivateNetwork=yes", unit)
        self.assertNotIn("AF_INET ", unit)
        self.assertNotIn("AF_INET6", unit)

    def test_changed_observer_wrong_schema_and_broken_console_payload_are_refused(self):
        self.journal.emit("stream_started", {})
        console_output = self.console.getvalue().decode()
        with self.assertRaisesRegex(host.ObservationError, "unreviewed"):
            collector.parse_console(console_output, expected_hash="c" * 64)
        with self.assertRaises(Exception):
            collector.parse_console(journal.PREFIX + "broken\n")
        with self.assertRaisesRegex(host.ObservationError, "not_available"):
            collector.parse_console("ordinary console boot output")

    def test_provider_timestamps_inside_console_chunks_preserve_exact_records(self):
        self.journal.emit("stream_started", {"executor_ready": False})
        self.journal.emit("heartbeat", {})
        plain = self.console.getvalue().decode()
        timestamp = "[2026-01-02T03:04:05.123456]"
        # Exercise framing before prefixes and in the middle of base64 payloads.
        for width in (17, 127, 1024):
            with self.subTest(width=width):
                chunked = timestamp + timestamp.join(plain[i:i + width] for i in range(0, len(plain), width))
                self.assertEqual(collector.parse_console(chunked), collector.parse_console(plain))

    def test_unknown_or_invalid_console_interleaving_is_not_repaired(self):
        self.journal.emit("stream_started", {})
        plain = self.console.getvalue().decode().strip()
        split = len(journal.PREFIX) + 15
        for injected in ("[truncated]", "[2026-99-02T03:04:05.123456]", "[2026-01-02T03:04:"):
            with self.subTest(injected=injected), self.assertRaises(Exception):
                collector.parse_console(plain[:split] + injected + plain[split:])

    def test_console_and_record_byte_limits_are_preserved(self):
        for text in ("x" * (1024 * 1024 + 1), "\u00e9" * (512 * 1024 + 1)):
            with self.subTest(length=len(text)), self.assertRaisesRegex(host.ObservationError, "console_output_too_large"):
                collector.parse_console(text)
        payload = base64.b64encode(b"x" * 12289).decode()
        with self.assertRaisesRegex(host.ObservationError, "console_record_too_large"):
            collector.parse_console(journal.PREFIX + payload + "\n")

    def test_private_journal_contains_the_same_ordered_safe_records(self):
        self.journal.emit("stream_started", {})
        self.journal.emit("heartbeat", {})
        self.file.seek(0)
        self.assertEqual([json.loads(line) for line in self.file], self.records())

    def test_existing_containers_refuse_readiness_before_an_event_stream_is_opened(self):
        connection = Mock()
        response = io.BytesIO(b'[{"Id":"existing-container"}]')
        response.status = 200
        connection.getresponse.return_value = response
        ready = Mock()
        with patch.object(host, "DockerConnection", return_value=connection):
            with self.assertRaisesRegex(host.ObservationError, "before_containers"):
                journal.watch(self.journal, ready=ready)
        ready.assert_not_called()
        connection.request.assert_called_once_with("GET", "/containers/json?all=true")
        self.assertEqual(self.console.getvalue(), b"")


class ProviderHostTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = {"vpc_id": "vpc-a", "private_subnet_ids": ["subnet-a"], "ami_id": "ami-a",
                         "job_security_group_ids": ["sg-a"], "group_a": 17, "group_b": 18}
        self.instance = {"InstanceId": "i-a", "VpcId": "vpc-a", "SubnetId": "subnet-a", "ImageId": "ami-a",
                         "SecurityGroups": [{"GroupId": "sg-a"}], "Tags": [
                             {"Key": "Component", "Value": "gitlab-job"}, {"Key": "Environment", "Value": "dev"},
                             {"Key": "GroupId", "Value": "17"}]}
        self.ec2 = Mock()

    def verify(self, change=None):
        self.ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{**self.instance, **(change or {})}]}]}
        return collector.verified_instance(self.fixtures, "i-a", self.ec2)

    def test_real_instance_scope_is_required_before_console_read(self):
        self.assertIsNone(self.verify()["instance_profile"])
        cases = [{"VpcId": "vpc-other"}, {"SubnetId": "subnet-other"}, {"ImageId": "ami-other"},
                 {"IamInstanceProfile": {"Arn": "unexpected"}}, {"PublicIpAddress": "198.51.100.1"},
                 {"SecurityGroups": [{"GroupId": "sg-other"}]}, {"Tags": []},
                 {"NetworkInterfaces": [{"Ipv6Addresses": [{"Ipv6Address": "2001:db8::1"}]}]}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(host.ObservationError):
                self.verify(change)
        self.ec2.get_console_output.assert_not_called()

    def test_collection_accepts_console_decoded_by_the_real_sdk_handler(self):
        self.fixtures.update(account_id="123456789012", run_id=BOOT, source_revision="b" * 40,
                             project_a=101, project_b=102)
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                region_name="us-east-1")
        ec2 = session.client("ec2")
        sts = session.client("sts")
        with tempfile.TemporaryFile() as output:
            console = io.BytesIO(b"ordinary private boot output\n")
            console.seek(0, io.SEEK_END)
            writer = journal.Journal(output, console, BOOT)
            record = writer.emit("stream_started", {})
            with Stubber(sts) as sts_stub, Stubber(ec2) as ec2_stub, patch.object(
                    session, "client", side_effect={"sts": sts, "ec2": ec2}.__getitem__):
                sts_stub.add_response("get_caller_identity", {"Account": self.fixtures["account_id"]}, {})
                ec2_stub.add_response("describe_instances", {"Reservations": [{"Instances": [self.instance]}]},
                                      {"InstanceIds": ["i-a"]})
                ec2_stub.add_response("get_console_output", {
                    "InstanceId": "i-a", "Output": base64.b64encode(console.getvalue()).decode(),
                    "Timestamp": datetime.now(timezone.utc), "ResponseMetadata": {"RequestId": "provider-request"}},
                    {"InstanceId": "i-a", "Latest": True})
                result = collector.collect(self.fixtures, "i-a", session)
                sts_stub.assert_no_pending_responses()
                ec2_stub.assert_no_pending_responses()
        self.assertEqual(result["records"], [record])
        self.assertEqual(result["history_status"], "contiguous_so_far")
        self.assertEqual(result["snapshots"][0]["aws_request_id"], "provider-request")
        self.assertEqual(result["snapshots"][0]["console_sha256"], hashlib.sha256(console.getvalue()).hexdigest())
        self.assertNotIn("ordinary private boot output", json.dumps(result))

    def test_provider_receipts_retain_actual_console_records_and_reject_changed_run(self):
        self.fixtures.update(account_id="123456789012", run_id=BOOT, source_revision="b" * 40,
                             project_a=101, project_b=102)
        self.verify()
        session = Mock()
        sts = Mock()
        sts.get_caller_identity.return_value = {"Account": self.fixtures["account_id"]}
        session.client.side_effect = {"sts": sts, "ec2": self.ec2}.__getitem__
        with tempfile.TemporaryFile() as output:
            console = io.BytesIO()
            writer = journal.Journal(output, console, BOOT)
            writer.emit("stream_started", {})
            self.ec2.get_console_output.return_value = {
                "InstanceId": "i-a", "Output": console.getvalue().decode(),
                "Timestamp": datetime.now(timezone.utc), "ResponseMetadata": {"RequestId": "provider-request"}}
            result = collector.collect(self.fixtures, "i-a", session)
            self.assertEqual(result["history_status"], "contiguous_so_far")
            self.assertEqual(result["snapshots"][0]["aws_request_id"], "provider-request")
            first_digest = hashlib.sha256(json.dumps(result["records"][0], sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(result["snapshots"][0]["record_sha256s"], [first_digest])
            self.assertNotEqual(result["acceptance"], "pass")
            repeated = collector.collect(self.fixtures, "i-a", session, result)
            self.assertEqual(len(repeated["records"]), 1)
            self.assertEqual(len(repeated["snapshots"]), 2)
            writer.emit("heartbeat", {"executor_ready": False})
            self.ec2.get_console_output.return_value.update(
                Output=console.getvalue().decode(), Timestamp=datetime.now(timezone.utc))
            extended = collector.collect(self.fixtures, "i-a", session, repeated)
            self.assertEqual(extended["snapshots"][0]["record_sha256s"], [first_digest])
            self.assertEqual(len(extended["snapshots"][-1]["record_sha256s"]), 2)
            with self.assertRaisesRegex(host.ObservationError, "scope_changed"):
                collector.collect({**self.fixtures, "run_id": "different"}, "i-a", session, result)
            writer.emit("container_event", {"project_id": 999, "source_revision": "b" * 40})
            self.ec2.get_console_output.return_value["Output"] = console.getvalue().decode()
            with self.assertRaisesRegex(host.ObservationError, "outside_owned"):
                collector.collect(self.fixtures, "i-a", session, result)


if __name__ == "__main__":
    unittest.main()


class IPv6MetadataProviderTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = {'account_id': '123456789012', 'run_id': BOOT, 'source_revision': 'a' * 40,
                         'vpc_id': 'vpc-a', 'private_subnet_ids': ['subnet-a'], 'ami_id': 'ami-a',
                         'asg_names': ['owned-asg']}
        self.instance = {'InstanceId': 'i-a', 'VpcId': 'vpc-a', 'SubnetId': 'subnet-a', 'ImageId': 'ami-a',
                         'MetadataOptions': {'State': 'applied', 'HttpProtocolIpv6': 'disabled', 'HttpTokens': 'required'},
                         'NetworkInterfaces': [{'NetworkInterfaceId': 'eni-a', 'Ipv6Addresses': []}]}
        self.membership = {'InstanceId': 'i-a', 'AutoScalingGroupName': 'owned-asg', 'AvailabilityZone': 'us-east-1a',
                           'LifecycleState': 'InService', 'HealthStatus': 'HEALTHY', 'ProtectedFromScaleIn': True,
                           'LaunchTemplate': {'LaunchTemplateId': 'lt-a', 'Version': '7'}}
        self.template = {'LaunchTemplateId': 'lt-a', 'VersionNumber': 7, 'LaunchTemplateData': {
            'ImageId': 'ami-a', 'MetadataOptions': {'HttpProtocolIpv6': 'disabled', 'HttpTokens': 'required'}}}

    def test_actual_sdk_collects_exact_instance_asg_and_template_version(self):
        session = boto3.Session(aws_access_key_id='testing', aws_secret_access_key='testing', region_name='us-east-1')
        ec2, asg = session.client('ec2'), session.client('autoscaling')
        with Stubber(ec2) as e, Stubber(asg) as a:
            e.add_response('describe_instances', {'Reservations': [{'Instances': [self.instance]}],
                'ResponseMetadata': {'RequestId': 'ec2-request'}}, {'InstanceIds': ['i-a']})
            a.add_response('describe_auto_scaling_instances', {'AutoScalingInstances': [self.membership],
                'ResponseMetadata': {'RequestId': 'asg-request'}}, {'InstanceIds': ['i-a']})
            e.add_response('describe_launch_template_versions', {'LaunchTemplateVersions': [self.template],
                'ResponseMetadata': {'RequestId': 'template-request'}}, {'LaunchTemplateId': 'lt-a', 'Versions': ['7']})
            receipt = collector.ipv6_metadata.collect(self.fixtures, 'i-a', ec2, asg)
            e.assert_no_pending_responses(); a.assert_no_pending_responses()
        self.assertEqual(receipt['launch_template']['version'], 7)
        self.assertEqual(receipt['instance']['metadata_ipv6'], 'disabled')
        collector.ipv6_metadata.validate(receipt, self.fixtures, 'i-a',
            datetime.fromisoformat(receipt['started_at']), datetime.fromisoformat(receipt['observed_at']))

    def test_missing_enabled_or_unscoped_provider_inputs_refuse_receipt(self):
        cases = [
            ('instance', lambda row: row.pop('MetadataOptions')),
            ('instance', lambda row: row['MetadataOptions'].update(HttpProtocolIpv6='enabled')),
            ('instance', lambda row: row['MetadataOptions'].update(State='pending')),
            ('instance', lambda row: row.update(IamInstanceProfile={'Arn': 'unexpected'})),
            ('instance', lambda row: row.pop('NetworkInterfaces')),
            ('instance', lambda row: row['NetworkInterfaces'][0].pop('Ipv6Addresses')),
            ('instance', lambda row: row['NetworkInterfaces'][0].update(Ipv6Addresses=[{'Ipv6Address': '2001:db8::1'}])),
            ('membership', lambda row: row.update(AutoScalingGroupName='another-asg')),
            ('membership', lambda row: row['LaunchTemplate'].update(Version='$Latest')),
            ('template', lambda row: row.update(VersionNumber=8)),
            ('template', lambda row: row['LaunchTemplateData']['MetadataOptions'].update(HttpProtocolIpv6='enabled')),
            ('template', lambda row: row['LaunchTemplateData'].update(IamInstanceProfile={'Arn': 'unexpected'})),
        ]
        for which, mutation in cases:
            rows = {name: copy.deepcopy(getattr(self, name)) for name in ('instance', 'membership', 'template')}
            mutation(rows[which])
            ec2, asg = Mock(), Mock()
            ec2.describe_instances.return_value = {'Reservations': [{'Instances': [rows['instance']]}], 'ResponseMetadata': {'RequestId': 'e'}}
            asg.describe_auto_scaling_instances.return_value = {'AutoScalingInstances': [rows['membership']], 'ResponseMetadata': {'RequestId': 'a'}}
            ec2.describe_launch_template_versions.return_value = {'LaunchTemplateVersions': [rows['template']], 'ResponseMetadata': {'RequestId': 't'}}
            with self.subTest(which=which, mutation=mutation), self.assertRaises((KeyError, ValueError)):
                collector.ipv6_metadata.collect(self.fixtures, 'i-a', ec2, asg)

    def test_only_terminated_instance_may_have_released_eni_details(self):
        retained = collector.ipv6_metadata.instance_facts(self.instance)
        released = {**self.instance, 'NetworkInterfaces': [], 'State': {'Name': 'terminated'}}
        collector.ipv6_metadata.current_instance_matches(released, retained)
        for change in ({'State': {'Name': 'running'}}, {'MetadataOptions': {}},
                       {'IamInstanceProfile': {'Arn': 'unexpected'}}, {'ImageId': 'ami-other'}):
            with self.subTest(change=change), self.assertRaises((KeyError, ValueError)):
                collector.ipv6_metadata.current_instance_matches({**released, **change}, retained)
