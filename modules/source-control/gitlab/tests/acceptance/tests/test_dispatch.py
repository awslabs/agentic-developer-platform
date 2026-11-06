import base64
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qualification_dispatch", ROOT / "dispatch.py")
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


class Admin:
    owner_id = 1

    def __init__(self, config, lock):
        self.config = config
        self.lock = lock
        self.project = None
        self.pipelines = []
        self.calls = []
        self.lose = None
        tenant = hashlib.sha256(config["tenant_id"].encode()).hexdigest()[:24]
        self.group = {"id": 7, "path": "tenant-" + tenant, "full_path": "tenant-" + tenant,
                      "description": "ADP tenant " + tenant, "parent_id": None}
        self.runner = {"runner_type": "group_type", "locked": True, "run_untagged": False,
                       "paused": False, "status": "online", "tag_list": ["adp-group-7"], "groups": [{"id": 7}]}
        self.variables = []
        self.branch_sha = config["source_revision"]
        self.source_changed = False

    async def pages(self, path, **params):
        self.calls.append(("GET", path))
        if path.endswith(("/members", "/members/all")):
            return [{"id": 1, "access_level": 50}]
        if path.endswith("/variables"):
            return self.variables
        if path.endswith("/pipelines"):
            return [{"id": p["id"]} for p in self.pipelines]
        raise AssertionError(path)

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if path == "groups/7":
            return copy.deepcopy(self.group)
        if method == "POST" and path == "projects":
            self.project = {**kwargs["json"], "id": 17, "namespace": {"id": 7, "kind": "group"},
                            "archived": False}
            if self.lose == "project":
                raise RuntimeError("lost synthetic response")
            return copy.deepcopy(self.project)
        if method == "GET" and (path == "projects/17" or path.startswith("projects/tenant-")):
            return copy.deepcopy(self.project)
        if path.startswith("projects/17/repository/branches/"):
            return {"commit": {"id": self.branch_sha}}
        if path.startswith("projects/17/repository/files/"):
            from urllib.parse import unquote
            content = (dispatch.REPO / unquote(path.split("/files/")[1])).read_bytes()
            if self.source_changed:
                content += b"changed"
            return {"encoding": "base64", "commit_id": self.config["source_revision"],
                    "content": base64.b64encode(content).decode()}
        if path == "runners/27":
            return self.runner
        if method == "POST" and path == "projects/17/pipeline":
            row = {"id": 37, "sha": self.config["source_revision"], "ref": kwargs["json"]["ref"],
                   "source": "api", "variables": kwargs["json"]["variables"]}
            self.pipelines.append(row)
            if self.lose == "pipeline":
                raise RuntimeError("lost synthetic response")
            return row
        if path == "projects/17/pipelines/37":
            return self.pipelines[0]
        if path == "projects/17/pipelines/37/variables":
            return self.pipelines[0]["variables"]
        raise AssertionError((method, path))


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.json"
        self.config = {"run_id": "11111111-1111-4111-8111-111111111111", "source_revision": "a" * 40,
                       "account_id": "123456789012", "expected_role_arn": "arn:aws:iam::123456789012:role/operator",
                       "region": "us-east-1", "gitlab_url": "https://gitlab.example.test/gitlab", "ca_file": "/private/ca",
                       "tenant_id": "qualification-test", "group_id": 7, "runner_id": 27,
                       "runner_tag": "adp-group-7", "ami_id": "ami-12345678"}
        self.targets = Path(self.temp.name) / "targets.json"
        names = sorted({"gateway", "devbox", "rds", "redis", "other_tenant", "metadata_ipv4",
                        "metadata_ipv6", "cluster_api", "sts", "secretsmanager", "internet",
                        "alternate_dns", "doh", "gitlab", "cache"})
        self.targets.write_text(json.dumps({"run_id": self.config["run_id"], "targets": [
            {"name": name, "address": "fd00:ec2::254" if name == "metadata_ipv6" else
             "169.254.169.254" if name == "metadata_ipv4" else "192.0.2.1",
             "port": 80 if name.startswith("metadata_") else 443, "kind": "https" if name in {"gitlab", "doh"} else
             "dns" if name == "alternate_dns" else "tcp",
             "expected": "allowed" if name in {"gitlab", "cache"} else "blocked",
             **({"hostname": "example.test", "expected_status": 200} if name in {"gitlab", "doh"} else {})}
            for name in names]}))
        self.targets.chmod(0o600)
        self.config["targets_file"] = str(self.targets)
        self.lock = {"job_image": "repo/job:test@sha256:" + "b" * 64,
                     "helper_image": "repo/helper:test@sha256:" + "c" * 64}
        self.admin = Admin(self.config, self.lock)
        self.journal = dispatch.Journal(self.path, "test-scope")
        self.addCleanup(self.journal.close)
        self.operation = dispatch.Qualification(self.config, self.lock, self.admin, self.journal)

    async def test_real_source_payload_and_one_pipeline_reconciles_without_resubmission(self):
        dispatch.validate_config(self.config)
        result = await self.operation.dispatch()
        self.assertEqual(result["pipeline_id"], 37)
        self.assertEqual(result["acceptance"], "not_evaluated")
        self.assertEqual(await self.operation.dispatch(), result)
        self.assertEqual(self.admin.calls.count(("POST", "projects")), 1)
        self.assertEqual(self.admin.calls.count(("POST", "projects/17/pipeline")), 1)
        self.assertEqual(dispatch.private_json(self.path)["phase"], "pipeline_created")
        self.assertEqual(self.admin.project["ci_config_path"], dispatch.CONFIG_PATH)
        self.assertFalse(self.admin.project["initialize_with_readme"])
        variables = self.admin.pipelines[0]["variables"]
        target = next(row for row in variables if row["key"] == "ADP_QUALIFICATION_TARGETS_FILE")
        self.assertEqual(target["variable_type"], "file")
        self.assertEqual(json.loads(target["value"])["run_id"], self.config["run_id"])

    async def test_private_target_drift_refused_before_pipeline_creation(self):
        original = self.targets.read_text()
        for change in (lambda payload: payload.update(run_id="other-run"),
                       lambda payload: payload["targets"].pop(),
                       lambda payload: payload["targets"][0].update(expected="allowed"),
                       lambda payload: payload["targets"][0].update(address="internal.example.test")):
            payload = json.loads(original)
            change(payload)
            self.targets.write_text(json.dumps(payload))
            with self.subTest(change=change), self.assertRaises((dispatch.DispatchError, ValueError)):
                await self.operation.dispatch()
        self.targets.write_text(original)
        self.targets.chmod(0o644)
        with self.assertRaisesRegex(dispatch.DispatchError, "private_operator_file_required"):
            await self.operation.dispatch()
        self.assertNotIn(("POST", "projects/17/pipeline"), self.admin.calls)

    async def test_metadata_target_substitutions_refused_before_pipeline_creation(self):
        original = self.targets.read_text()
        for name, field, value in (("metadata_ipv4", "address", "169.254.169.253"),
                                   ("metadata_ipv6", "address", "fd00:ec2::123"),
                                   ("metadata_ipv4", "port", 443), ("metadata_ipv6", "port", 443)):
            with self.subTest(name=name, field=field):
                payload = json.loads(original)
                next(row for row in payload["targets"] if row["name"] == name)[field] = value
                self.targets.write_text(json.dumps(payload))
                with self.assertRaisesRegex(dispatch.DispatchError, "qualification_target_protocol_wrong"):
                    await self.operation.dispatch()
        self.assertNotIn(("POST", "projects/17/pipeline"), self.admin.calls)

    async def test_ipv4_only_mode_is_bound_to_the_exact_pipeline_target(self):
        payload = json.loads(self.targets.read_text())
        next(row for row in payload['targets'] if row['name'] == 'metadata_ipv6')['expected'] = 'disabled_by_provider'
        self.targets.write_text(json.dumps(payload))
        self.operation = dispatch.Qualification(self.config, self.lock, self.admin, self.journal)
        await self.operation.dispatch()
        bound = next(row for row in self.admin.pipelines[0]['variables'] if row['key'] == 'ADP_QUALIFICATION_TARGETS_FILE')
        self.assertEqual(json.loads(bound['value']), payload)
        next(row for row in payload['targets'] if row['name'] == 'metadata_ipv4')['expected'] = 'disabled_by_provider'
        self.targets.write_text(json.dumps(payload))
        with self.assertRaisesRegex(dispatch.DispatchError, 'qualification_targets_invalid'):
            dispatch.target_payload(self.config)

    async def test_pipeline_file_binding_cannot_be_replaced_with_environment_variable(self):
        await self.operation.dispatch()
        variables = self.admin.pipelines[0]["variables"]
        target = next(row for row in variables if row["key"] == "ADP_QUALIFICATION_TARGETS_FILE")
        target["variable_type"] = "env_var"
        with self.assertRaisesRegex(dispatch.DispatchError, "pipeline_bindings_wrong"):
            await self.operation.dispatch()
        self.assertEqual(self.admin.calls.count(("POST", "projects/17/pipeline")), 1)

    async def test_lost_project_response_reuses_owned_project(self):
        self.admin.lose = "project"
        with self.assertRaises(RuntimeError):
            await self.operation.prepare()
        self.assertEqual(self.journal.data["phase"], "project_intent")
        result = await self.operation.prepare()
        self.assertEqual(result["project_id"], 17)
        self.assertEqual(self.admin.calls.count(("POST", "projects")), 1)

    async def test_lost_pipeline_response_reconciles_but_never_redispatches(self):
        self.admin.lose = "pipeline"
        with self.assertRaises(RuntimeError):
            await self.operation.dispatch()
        self.assertEqual(self.journal.data["phase"], "pipeline_intent")
        await self.operation.dispatch()
        self.assertEqual(self.admin.calls.count(("POST", "projects/17/pipeline")), 1)

    async def test_ambiguous_absent_pipeline_stays_blocked(self):
        self.admin.lose = "pipeline"
        with self.assertRaises(RuntimeError):
            await self.operation.dispatch()
        self.admin.pipelines = []
        with self.assertRaisesRegex(dispatch.DispatchError, "ambiguous"):
            await self.operation.dispatch()
        self.assertEqual(self.admin.calls.count(("POST", "projects/17/pipeline")), 1)

    async def test_conflicting_project_is_not_modified(self):
        await self.operation.prepare()
        self.admin.project["description"] = "existing tenant project"
        before = len(self.admin.calls)
        with self.assertRaisesRegex(dispatch.DispatchError, "owned_project"):
            await self.operation.dispatch()
        self.assertTrue(all(method == "GET" for method, _ in self.admin.calls[before:]))

    async def test_unjournaled_project_is_not_adopted(self):
        await self.operation.prepare()
        self.journal.save(phase="new")
        with self.assertRaisesRegex(dispatch.DispatchError, "this_journal"):
            await self.operation.prepare()

    async def test_wrong_pushed_sha_and_source_bytes_prevent_dispatch(self):
        for field, value in (("branch_sha", "d" * 40), ("source_changed", True)):
            with self.subTest(field=field):
                original = getattr(self.admin, field)
                setattr(self.admin, field, value)
                with self.assertRaises(dispatch.DispatchError):
                    await self.operation.dispatch()
                setattr(self.admin, field, original)
        self.assertNotIn(("POST", "projects/17/pipeline"), self.admin.calls)

    async def test_wrong_runner_and_variable_override_prevent_dispatch(self):
        self.admin.runner["groups"] = [{"id": 8}]
        with self.assertRaisesRegex(dispatch.DispatchError, "runner_not_ready"):
            await self.operation.dispatch()
        self.admin.runner["groups"] = [{"id": 7}]
        self.admin.variables = [{"key": "ADP_QUALIFICATION_JOB_IMAGE", "value": "substituted"}]
        with self.assertRaisesRegex(dispatch.DispatchError, "variable_override"):
            await self.operation.dispatch()
        self.assertNotIn(("POST", "projects/17/pipeline"), self.admin.calls)

    async def test_existing_pipeline_with_different_binding_is_not_adopted(self):
        await self.operation.dispatch()
        self.admin.pipelines[0]["variables"][0]["value"] = "another-run"
        with self.assertRaisesRegex(dispatch.DispatchError, "bindings_wrong"):
            await self.operation.dispatch()
        self.assertEqual(self.admin.calls.count(("POST", "projects/17/pipeline")), 1)

    def test_journal_lock_and_scope_are_exclusive(self):
        self.journal.save(phase="project_intent")
        with self.assertRaises(BlockingIOError):
            dispatch.Journal(self.path, "test-scope")
        self.journal.close()
        with self.assertRaisesRegex(dispatch.DispatchError, "scope_conflict"):
            dispatch.Journal(self.path, "another-scope")


if __name__ == "__main__":
    unittest.main()
