"""Offline campaign tool ownership, partial setup and cleanup fixtures."""

import copy
import datetime
import hashlib
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE = Path(__file__).resolve().parents[2] / "poc/devlake"


class ToolSourceLifecycleTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(MODULE))
        self.addCleanup(lambda: sys.path.remove(str(MODULE)))
        import tool_sources

        self.tools = tool_sources
        now = datetime.datetime.now(datetime.timezone.utc)
        self.revision = "a" * 40
        self.record = {"pilot_id": "devlake-" + self.revision, "finalizer_revision": self.revision,
                       "expires_at": (now + datetime.timedelta(days=1)).isoformat(),
                       "operator": "fixture-operator", "account": "000000000000", "region": "us-east-1"}
        namespace = "s14-" + self.record["pilot_id"]
        resources = [{"kind": "Deployment", "namespace": namespace, "name": "s14-" + tool}
                     for tool in self.tools.TOOLS]
        resources.append({"kind": "Namespace", "namespace": "", "name": namespace})
        self.config = {"pilot_id": self.record["pilot_id"], "revision": self.revision,
                       "expires_at": self.record["expires_at"], "operator": self.record["operator"],
                       "account": self.record["account"], "region": self.record["region"],
                       "namespace": namespace, "cluster_ref": "sha256:" + "f" * 64, "context": "approved-context",
                       "images": {tool: "registry.invalid/" + tool + "@sha256:" + character * 64
                                  for tool, character in zip(self.tools.TOOLS, "abc")},
                       "resources": resources, "bindings": {provider: None for provider in self.tools.TOOLS + self.tools.EXTERNAL}}
        self.capacity = {"cluster_ref": "sha256:" + "f" * 64, "context": "approved-context", "free_cpu": 2,
                         "free_gib": 8, "free_disk_gib": 20, "observed_at": now.isoformat()}
        self.objects = {}
        self.created = []
        self.deleted = []
        self.saved = 0

    def inspect(self, resource):
        return self.objects.get(self.tools.identity(None, resource))

    def manifest(self, intent, resource):
        manifest = {"kind": resource["kind"], "metadata": {"name": resource["name"],
                    "labels": {"adp.dev/pilot": intent["pilot_id"], "adp.dev/revision": intent["revision"]}}}
        if resource["namespace"]:
            manifest["metadata"]["namespace"] = resource["namespace"]
        if resource["kind"] == "Deployment":
            tool = resource["name"].removeprefix("s14-")
            manifest["spec"] = {"template": {"spec": {"containers": [{"image": intent["images"][tool]}]}}}
        return manifest

    def create(self, manifest):
        resource = next(item for item in self.config["resources"] if
                        item["kind"] == manifest["kind"] and item["name"] == manifest["metadata"]["name"])
        key = self.tools.identity(None, resource)
        self.created.append(key)
        self.objects[key] = manifest

    def delete(self, resource):
        key = self.tools.identity(None, resource)
        self.deleted.append(key)
        self.objects.pop(key)

    def test_register_before_partial_setup_and_readback_unavailable_sources(self):
        current = {"record": self.record}
        def read(role, **kwargs):
            self.assertEqual(role, "finalizer")
            return ("scope",), "before", copy.deepcopy(current["record"])
        def save(scope, previous, record):
            self.saved += 1
            current["record"] = copy.deepcopy(record)
        with mock.patch.object(self.tools.finalize, "get_registered", side_effect=read), \
             mock.patch.object(self.tools.finalize, "save", side_effect=save):
            intent = self.tools.register(self.config, self.capacity)
            self.assertEqual(self.saved, 1)
            self.tools.validate(current["record"]["tool_sources"], current["record"])
            self.assertEqual(self.tools.register(self.config, self.capacity), intent)
            self.assertEqual(self.saved, 1)
            with self.assertRaisesRegex(ValueError, "cannot be replaced"):
                self.tools.register({**self.config, "cluster_ref": "sha256:" + "e" * 64},
                                    {**self.capacity, "cluster_ref": "sha256:" + "e" * 64})
        self.create(self.manifest(intent, self.config["resources"][-1]))
        manifests = {self.tools.identity(None, item): self.manifest(intent, item)
                     for item in self.config["resources"]}
        attempts = []
        def interrupted():
            attempts.append(True)
            if len(attempts) == 3:
                raise ValueError("admission revoked")
        def check_capacity():
            interrupted()
            return self.capacity
        with mock.patch.object(self.tools.finalize, "get_registered", side_effect=read):
            with self.assertRaisesRegex(ValueError, "revoked"):
                self.tools.setup(intent, self.inspect, self.create, manifests, check_capacity)
            self.assertEqual(len(self.created), 2)
            self.tools.setup(intent, self.inspect, self.create, manifests, lambda: self.capacity)
        self.assertEqual(len(self.created), 4)
        result = self.tools.read_sources(intent, self.inspect, lambda *_: self.fail("unbound reader called"))
        self.assertTrue(all(result[self.tools.identity(None, item)] == "resource-present"
                            for item in self.config["resources"]))
        self.assertTrue(all(result[provider] == "unavailable" for provider in self.tools.EXTERNAL))
        intent["bindings"]["gitlab"] = {"scope_id": "91", "reader_ref": "reader-ref", "consent_ref": "consent-ref", "owner_ref": "owned-test-source"}
        self.assertEqual(self.tools.read_sources(intent, self.inspect, lambda provider, binding: "wrong")["gitlab"], "unavailable")
        self.assertEqual(self.tools.read_sources(intent, self.inspect, lambda provider, binding: "91")["gitlab"], "available")

    def test_selected_tool_seed_uses_separate_bootstrap_and_reader(self):
        binding = {"scope_id": "quality-project", "reader_ref": "reader-ref",
                   "consent_ref": "consent-ref", "bootstrap_ref": "bootstrap-ref"}
        self.config["bindings"]["sonarqube"] = binding
        intent = self.tools.plan(self.config, self.record, self.capacity)
        self.record["tool_sources"] = intent
        resource = self.config["resources"][0]
        self.create(self.manifest(intent, resource))
        writes = []
        def read(role, **kwargs):
            return ("scope",), "previous", self.record
        with mock.patch.object(self.tools.finalize, "get_registered", side_effect=read):
            statuses = self.tools.seed_sources(intent, self.inspect,
                lambda tool, selection: writes.append((tool, selection["bootstrap_ref"])),
                lambda tool, selection: selection["scope_id"], lambda: None)
            self.assertEqual(statuses, {"sonarqube": "available", "jenkins": "unavailable", "argocd": "unavailable"})
            self.assertEqual(writes, [("sonarqube", "bootstrap-ref")])
            self.record["finalization_requested"] = {"reason": "abort"}
            with self.assertRaisesRegex(ValueError, "authority"):
                self.tools.seed_sources(intent, self.inspect,
                    lambda *_: self.fail("revoked bootstrap"), lambda *_: None, lambda: None)
            self.assertEqual(writes, [("sonarqube", "bootstrap-ref")])

    def test_cleanup_resumes_without_deleting_unrelated_resource(self):
        intent = self.tools.plan(self.config, self.record, self.capacity)
        for resource in [*self.config["resources"][:2], self.config["resources"][-1]]:
            self.create(self.manifest(intent, resource))
        self.created.clear()
        outsider = {"kind": "ConfigMap", "metadata": {"name": "other-task", "namespace": intent["namespace"]}}
        outsider_key = self.tools.identity(None, {"kind": "ConfigMap", "name": "other-task", "namespace": intent["namespace"]})
        self.objects[outsider_key] = outsider
        before = self.tools.read_sources(intent, self.inspect, lambda *_: self.fail("unbound reader called"))
        self.assertEqual(before[self.tools.identity(None, self.config["resources"][2])], "unavailable")
        self.assertTrue(all(before[provider] == "unavailable" for provider in self.tools.EXTERNAL))
        def empty(intent):
            if outsider_key in self.objects:
                raise ValueError("namespace contains resources outside owned inventory")
        with self.assertRaisesRegex(ValueError, "outside owned inventory"):
            self.tools.cleanup(intent, self.inspect, self.delete, lambda: None, empty)
        self.assertEqual(len(self.deleted), 2)
        self.assertIsNotNone(self.inspect(self.config["resources"][-1]))
        self.assertIs(self.objects[outsider_key], outsider)
        self.assertNotIn(outsider_key, self.deleted)
        self.objects.pop(outsider_key)
        self.tools.cleanup(intent, self.inspect, self.delete, lambda: None, empty)
        self.assertEqual(len(self.deleted), 3)
        self.assertEqual(len(intent["receipts"]), 4)
        self.tools.cleanup(intent, self.inspect, self.delete, lambda: self.fail("duplicate receipt"), empty)
        self.assertEqual(len(self.deleted), 3)
        self.tools.validate(intent, self.record)


    def test_explicit_kube_context_fingerprint_and_cleanup_rights(self):
        intent = self.tools.plan(self.config, self.record, self.capacity)
        cluster = {"name": "selected", "cluster": {"server": "https://private.invalid"}}
        intent["cluster_ref"] = "sha256:" + hashlib.sha256(json.dumps(
            cluster, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        responses = []
        def command(arguments, **kwargs):
            responses.append(arguments)
            output = (json.dumps({"clusters": [cluster], "contexts": [{"name": intent["context"]}]}).encode()
                      if "config" in arguments else b"yes")
            return mock.Mock(stdout=output)
        with tempfile.TemporaryDirectory() as directory:
            kubeconfig = Path(directory) / "private-kubeconfig"
            kubeconfig.write_text("selected test context")
            kubeconfig.chmod(0o600)
            with mock.patch.dict(os.environ, {"ADP_DEVLAKE_TOOL_KUBECONFIG": str(kubeconfig)}), \
                 mock.patch.object(self.tools.subprocess, "run", side_effect=command):
                client = self.tools.Kubernetes(intent)
                client.qualify(self.config["resources"])
                self.assertTrue(all("--kubeconfig" in arguments and "--context" in arguments
                                    for arguments in responses))
                self.assertEqual(len(responses), 1 + 5 * len(self.config["resources"]))
                intent["cluster_ref"] = "sha256:" + "e" * 64
                with self.assertRaisesRegex(ValueError, "differs"):
                    self.tools.Kubernetes(intent)

    def test_existing_namespace_inventory_refuses_unrelated_objects(self):
        intent = self.tools.plan(self.config, self.record, self.capacity)
        client = object.__new__(self.tools.Kubernetes)
        def response(*arguments):
            if arguments[0] == "api-resources":
                return b"configmaps\n"
            return json.dumps({"items": [{"kind": "ConfigMap", "metadata": {
                "name": "other-task", "namespace": intent["namespace"]}}]}).encode()
        with mock.patch.object(client, "call", side_effect=response):
            with self.assertRaisesRegex(ValueError, "outside owned inventory"):
                client.inventory(intent)

    def test_rejects_foreign_existing_resource_and_unpinned_versions(self):
        intent = self.tools.plan(self.config, self.record, self.capacity)
        resource = self.config["resources"][0]
        foreign = self.manifest(intent, resource)
        foreign["metadata"]["labels"]["adp.dev/pilot"] = "other"
        self.objects[self.tools.identity(None, resource)] = foreign
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.tools.cleanup(intent, self.inspect, self.delete, lambda: None, lambda _: None)
        self.assertEqual(self.deleted, [])
        with self.assertRaisesRegex(ValueError, "pinned digests"):
            self.tools.plan({**self.config, "images": {**self.config["images"], "jenkins": "latest"}},
                            self.record, self.capacity)
        with self.assertRaisesRegex(ValueError, "headroom"):
            self.tools.plan(self.config, self.record, {**self.capacity, "free_cpu": 1})
        with self.assertRaisesRegex(ValueError, "headroom"):
            self.tools.plan(self.config, self.record, {**self.capacity, "observed_at": (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).isoformat()})
