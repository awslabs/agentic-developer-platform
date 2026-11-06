"""Register and reconcile only explicitly owned campaign tool resources."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import finalize


TOOLS = ("sonarqube", "jenkins", "argocd")
EXTERNAL = ("gitlab", "pagerduty", "linear", "slack")
KINDS = ("Deployment", "Service", "ConfigMap", "Job", "Namespace")
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
IMAGE = re.compile(r"[^\s]+@sha256:[a-f0-9]{64}\Z")


def plan(config, record, capacity, now=None):
    if not all(isinstance(value, dict) for value in (config, record, capacity)):
        raise ValueError("tool selection and headroom must be private objects")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    namespace = "s14-" + record["pilot_id"]
    if (config.get("pilot_id") != record["pilot_id"] or
            config.get("revision") != record["finalizer_revision"] or
            config.get("expires_at") != record["expires_at"] or
            config.get("operator") != record["operator"] or
            config.get("account") != record["account"] or
            config.get("region") != record["region"] or
            config.get("namespace") != namespace or not NAME.fullmatch(namespace)):
        raise ValueError("tool source target differs from registered campaign")
    if (capacity.get("cluster_ref") != config.get("cluster_ref") or
            capacity.get("context") != config.get("context") or
            not isinstance(config.get("cluster_ref"), str) or
            not re.fullmatch(r"sha256:[a-f0-9]{64}", config["cluster_ref"]) or
            not config.get("context") or
            any(type(capacity.get(key)) is not int or capacity[key] < required
                for key, required in (("free_cpu", 2), ("free_gib", 8), ("free_disk_gib", 20))) or
            not 0 <= (now - finalize.timestamp(capacity["observed_at"])).total_seconds() <= 3600):
        raise ValueError("selected cluster headroom or version preflight is unavailable")
    if set(config.get("images", {})) != set(TOOLS) or any(
            not isinstance(image, str) or not IMAGE.fullmatch(image)
            for image in config["images"].values()):
        raise ValueError("all tool images require pinned digests")
    resources = config.get("resources")
    if (not isinstance(resources, list) or not 4 <= len(resources) <= 24 or
            resources[-1] != {"kind": "Namespace", "name": namespace, "namespace": ""}):
        raise ValueError("exact owned tool inventory is required")
    seen = set()
    for item in resources:
        if (not isinstance(item, dict) or set(item) != {"kind", "name", "namespace"} or
                item["kind"] not in KINDS or not isinstance(item["name"], str) or
                not NAME.fullmatch(item["name"]) or
                item["namespace"] != ("" if item["kind"] == "Namespace" else namespace) or
                (item["kind"] != "Namespace" and not item["name"].startswith("s14-"))):
            raise ValueError("tool resource is outside the registered namespace")
        identity = (item["kind"], item["name"], item["namespace"])
        if identity in seen:
            raise ValueError("duplicate owned tool resource")
        seen.add(identity)
    if not all(("Deployment", "s14-" + tool, namespace) in seen for tool in TOOLS):
        raise ValueError("all three owned tools require pinned deployments")
    bindings = config.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != set(TOOLS + EXTERNAL) or any(
            value is not None and (not isinstance(value, dict) or
                                   set(value) != ({"scope_id", "reader_ref", "consent_ref", "bootstrap_ref"}
                                                  if provider in TOOLS else
                                                  ({"scope_id", "reader_ref", "consent_ref", "owner_ref"}
                                                   if provider == "gitlab" else
                                                   {"scope_id", "reader_ref", "consent_ref"})) or
                                   any(not isinstance(field, str) or not field for field in value.values()))
            for provider, value in bindings.items()):
        raise ValueError("external source requires a selected scope, reader and consent")
    return {"pilot_id": record["pilot_id"], "revision": record["finalizer_revision"],
            "expires_at": record["expires_at"], "operator": record["operator"],
            "account": record["account"], "region": record["region"],
            "cluster_ref": config["cluster_ref"], "context": config["context"],
            "namespace": namespace, "images": config["images"], "resources": resources,
            "bindings": bindings, "headroom": capacity, "receipts": {}}


def validate(intent, record):
    if not isinstance(intent, dict) or set(intent) != {
            "pilot_id", "revision", "expires_at", "operator", "account", "region",
            "cluster_ref", "context", "namespace", "images", "resources", "bindings", "headroom", "receipts"}:
        raise ValueError("tool source ledger differs from registered intent")
    config = {key: intent[key] for key in ("pilot_id", "revision", "expires_at", "operator",
                                           "account", "region", "cluster_ref", "context", "namespace",
                                           "images", "resources", "bindings")}
    expected = plan(config, record, intent["headroom"], finalize.timestamp(intent["headroom"]["observed_at"]))
    if {**expected, "receipts": intent["receipts"]} != intent or not isinstance(intent["receipts"], dict):
        raise ValueError("tool source ledger changed")
    keys = {f'{item["kind"]}/{item["namespace"]}/{item["name"]}' for item in intent["resources"]}
    if set(intent["receipts"]) - keys or any(value != "absent" for value in intent["receipts"].values()):
        raise ValueError("tool source absence receipt differs from owned inventory")
    if record.get("finalization_complete") and set(intent["receipts"]) != keys:
        raise ValueError("tool source cleanup is incomplete")


def register(config, capacity):
    scope, previous, record = finalize.get_registered("finalizer")
    if record.get("finalization_requested") or record.get("finalization_complete"):
        raise ValueError("terminal campaign cannot register tool sources")
    intent = plan(config, record, capacity)
    current = record.get("tool_sources")
    if current is not None:
        if current != intent:
            raise ValueError("tool source intent cannot be replaced")
        return current
    record["tool_sources"] = intent
    try:
        finalize.save(scope, previous, record)
    except (OSError, ValueError):
        _, _, stored = finalize.get_registered("finalizer")
        if stored.get("tool_sources") != intent:
            raise ValueError("protected tool source intent update was not confirmed") from None
    _, _, stored = finalize.get_registered("finalizer")
    if stored.get("tool_sources") != intent:
        raise ValueError("protected tool source intent readback differs")
    return intent


def identity(intent, resource):
    return f'{resource["kind"]}/{resource["namespace"]}/{resource["name"]}'


def owned(intent, resource, observed):
    labels = observed.get("metadata", {}).get("labels", {})
    if (observed.get("kind") != resource["kind"] or
            observed.get("metadata", {}).get("name") != resource["name"] or
            observed.get("metadata", {}).get("namespace", "") != resource["namespace"] or
            labels.get("adp.dev/pilot") != intent["pilot_id"] or
            labels.get("adp.dev/revision") != intent["revision"]):
        raise ValueError("tool resource is not owned by this campaign")
    if resource["kind"] == "Deployment":
        tool = resource["name"].removeprefix("s14-")
        containers = observed.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if tool not in intent["images"] or [container.get("image") for container in containers] != [intent["images"][tool]]:
            raise ValueError("tool deployment differs from pinned image")


def setup(intent, inspect, create, manifests, authorize):
    if not isinstance(manifests, dict) or set(manifests) != {
            identity(intent, item) for item in intent["resources"]}:
        raise ValueError("tool manifests differ from exact owned inventory")
    for resource in reversed(intent["resources"]):
        _, _, record = finalize.get_registered("finalizer")
        if (record.get("finalization_requested") or record.get("finalization_complete") or
                record.get("tool_sources") != intent):
            raise ValueError("current tool source authority differs")
        capacity = authorize()
        config = {key: intent[key] for key in ("pilot_id", "revision", "expires_at", "operator",
                                               "account", "region", "cluster_ref", "context", "namespace",
                                               "images", "resources", "bindings")}
        plan(config, record, capacity)
        observed = inspect(resource)
        if observed is not None:
            owned(intent, resource, observed)
            continue
        manifest = manifests[identity(intent, resource)]
        owned(intent, resource, manifest)
        if resource["kind"] == "Deployment":
            pod = manifest.get("spec", {}).get("template", {}).get("spec", {})
            if pod.get("hostNetwork") or any(
                    "hostPath" in volume
                    for volume in pod.get("volumes", [])):
                raise ValueError("tool manifest requests host networking or Docker socket")
        create(manifest)
        observed = inspect(resource)
        if observed is None:
            raise ValueError("tool creation was not independently observed")
        owned(intent, resource, observed)


def seed_sources(intent, inspect, bootstrap, reader, authorize):
    result = {}
    for tool in TOOLS:
        binding = intent["bindings"][tool]
        if binding is None:
            result[tool] = "unavailable"
            continue
        _, _, record = finalize.get_registered("finalizer")
        if record.get("tool_sources") != intent or record.get("finalization_requested"):
            raise ValueError("current tool source authority differs")
        authorize()
        selected = next(item for item in intent["resources"] if
                        item["kind"] == "Deployment" and item["name"] == "s14-" + tool)
        observed = inspect(selected)
        if observed is None:
            result[tool] = "unavailable"
            continue
        owned(intent, selected, observed)
        bootstrap(tool, binding)
        try:
            observed_scope = reader(tool, binding)
        except (OSError, ValueError):
            observed_scope = None
        result[tool] = "available" if observed_scope == binding["scope_id"] else "unavailable"
    return result


def read_sources(intent, inspect, reader):
    result = {}
    for resource in intent["resources"]:
        observed = inspect(resource)
        if observed is None:
            result[identity(intent, resource)] = "unavailable"
        else:
            owned(intent, resource, observed)
            result[identity(intent, resource)] = "resource-present"
    for provider, binding in intent["bindings"].items():
        if binding is None:
            result[provider] = "unavailable"
            continue
        try:
            observed = reader(provider, binding)
        except (OSError, ValueError):
            result[provider] = "unavailable"
            continue
        result[provider] = "available" if observed == binding["scope_id"] else "unavailable"
    return result


class Kubernetes:
    def __init__(self, intent):
        path = Path(os.environ.get("ADP_DEVLAKE_TOOL_KUBECONFIG", "")).resolve()
        if (not path.is_file() or path.is_relative_to(finalize.publish.ROOT) or
                path.stat().st_mode & 0o077):
            raise ValueError("explicit private tool kubeconfig is required")
        self.prefix = ["kubectl", "--kubeconfig", str(path), "--context", intent["context"]]
        config = json.loads(self.call("config", "view", "--minify", "-o", "json"))
        clusters = config.get("clusters", [])
        contexts = config.get("contexts", [])
        if (len(clusters) != 1 or len(contexts) != 1 or
                contexts[0].get("name") != intent["context"] or
                not clusters[0].get("cluster", {}).get("server", "").startswith("https://")):
            raise ValueError("selected tool cluster context is unavailable")
        fingerprint = "sha256:" + hashlib.sha256(json.dumps(
            clusters[0], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint != intent["cluster_ref"]:
            raise ValueError("selected tool cluster differs from registered target")

    def call(self, *args, data=None):
        result = subprocess.run([*self.prefix, *args], input=data, capture_output=True, timeout=60, check=True)
        if len(result.stdout) > 1024 * 1024:
            raise ValueError("tool resource readback exceeded bound")
        return result.stdout

    def qualify(self, resources):
        for resource in resources:
            namespace = [] if not resource["namespace"] else ["--namespace", resource["namespace"]]
            for verb in ("get", "list", "create", "patch", "delete"):
                if self.call("auth", "can-i", verb, resource["kind"].lower(), *namespace).strip() != b"yes":
                    raise ValueError("selected tool cleanup or setup authority is unavailable")

    def inspect(self, resource):
        result = self.call("get", resource["kind"].lower(), resource["name"],
                           *([] if not resource["namespace"] else ["--namespace", resource["namespace"]]),
                           "--ignore-not-found=true", "-o", "json")
        return json.loads(result) if result.strip() else None

    def create(self, manifest):
        self.call("apply", "--server-side", "-f", "-", data=json.dumps(manifest).encode())

    def delete(self, resource):
        self.call("delete", resource["kind"].lower(), resource["name"],
                  *([] if not resource["namespace"] else ["--namespace", resource["namespace"]]),
                  "--wait=true", "--timeout=50s")

    def inventory(self, intent):
        kinds = self.call("api-resources", "--verbs=list", "--namespaced", "-o", "name").decode().splitlines()
        if not kinds:
            raise ValueError("namespace inventory is unavailable")
        known = {(resource["kind"], resource["name"]): resource for resource in intent["resources"]}
        found = 0
        for kind in kinds:
            listing = json.loads(self.call("get", kind, "--namespace", intent["namespace"], "-o", "json"))
            if not isinstance(listing.get("items"), list):
                raise ValueError("namespace inventory is incomplete")
            for item in listing["items"]:
                key = (item.get("kind"), item.get("metadata", {}).get("name"))
                if key in (("ServiceAccount", "default"), ("ConfigMap", "kube-root-ca.crt")):
                    continue
                resource = known.get(key)
                if resource is None:
                    raise ValueError("namespace contains resources outside owned inventory")
                owned(intent, resource, item)
                found += 1
        return found

    def empty(self, intent):
        if self.inventory(intent):
            raise ValueError("namespace contains resources outside owned inventory")


def cleanup(intent, inspect, delete, save, empty):
    for resource in intent["resources"]:
        key = identity(intent, resource)
        observed = inspect(resource)
        if observed is not None:
            owned(intent, resource, observed)
            if resource["kind"] == "Namespace":
                empty(intent)
            delete(resource)
        if inspect(resource) is not None:
            raise ValueError("owned tool resource remains after deletion")
        if intent["receipts"].get(key) != "absent":
            intent["receipts"][key] = "absent"
            save()


def complete(intent):
    return set(intent["receipts"]) == {identity(intent, item) for item in intent["resources"]}


def finalize_owned():
    scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
    intent = record["tool_sources"]
    validate(intent, record)
    client = Kubernetes(intent)

    def save():
        nonlocal scope, previous, record, intent
        record["tool_sources"]["receipts"] = dict(intent["receipts"])
        finalize.save(scope, previous, record)
        scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
        if record["tool_sources"]["receipts"] != intent["receipts"]:
            raise ValueError("tool cleanup receipt readback differs")

    cleanup(intent, client.inspect, client.delete, save, client.empty)


def private_json(path):
    selected = Path(path).resolve()
    if (not selected.is_file() or selected.is_relative_to(finalize.publish.ROOT) or
            selected.stat().st_mode & 0o077 or selected.stat().st_size > 1024 * 1024):
        raise ValueError("tool source configuration must be private and outside the repository")
    return json.loads(selected.read_text())


def main():
    parser = argparse.ArgumentParser(description="Protected campaign tool setup and scoped source readback")
    parser.add_argument("--config", required=True)
    parser.add_argument("--capacity", required=True)
    parser.add_argument("--manifests", required=True)
    parser.add_argument("--setup", action="store_true")
    arguments = parser.parse_args()
    try:
        config = private_json(arguments.config)
        intent = register(config, private_json(arguments.capacity))
        client = Kubernetes(intent)
        manifests = private_json(arguments.manifests)
        if arguments.setup:
            client.qualify(intent["resources"])
            if (client.inspect(intent["resources"][-1]) is not None and
                    not any(client.inspect(item) is not None for item in intent["resources"][:-1])):
                client.inventory(intent)
            setup(intent, client.inspect, client.create, manifests,
                  lambda: private_json(arguments.capacity))
        result = read_sources(intent, client.inspect, lambda *_: None)
        print(json.dumps(result, sort_keys=True))
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
