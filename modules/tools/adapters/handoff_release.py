"""Switch prepared private connector routes only while core admission is fenced."""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import catalogue_binding
import guard_release

CONNECTORS = {"atlassian", "jira", "github", "slack"}
MAX_DOCUMENT = 1024 * 1024
# The foundation stage installs and verifies the adapter image under its own
# release group. The connector release group is only adopted later, inside the
# core admission fence, so a prepared runtime legitimately still carries the
# foundation group while serving the exact configured image.
FOUNDATION_GROUP = "atlassian-foundation"


def require(condition):
    if not condition:
        raise ValueError("adapter_handoff_refused")


def stage_configuration(value):
    """Return the actual stage configuration without boto3 transport metadata.

    Two unchanged consecutive reads of the same real stage differ only by
    ResponseMetadata, which carries a fresh RequestId (and HTTP headers) per
    call, so comparing whole responses can never confirm a stable stage against
    live API Gateway. Every actual stage field - deploymentId, canarySettings,
    variables and the rest - is retained, so a genuine deployment, canary or
    configuration change between the two reads is still rejected.
    """
    require(type(value) is dict)
    return {key: item for key, item in value.items() if key != "ResponseMetadata"}


def connector_configuration(value):
    """Return the validated deployed contract, refusing a foundation-only runtime.

    Private connector routes carry real provider authority, so neither
    preparation nor the fenced transition may assume it. Validation runs through
    the shipped AdapterConfiguration model so the authenticated schemas, their
    canonical hashes and the deployment-owned roles are all checked, not just a
    few keys. A runtime still in foundation mode has no observation
    configuration and must not be handed the connector routes.
    """
    from tool_adapters.config import AdapterConfiguration

    try:
        configuration = AdapterConfiguration.model_validate(value)
    except Exception:
        raise ValueError("adapter_handoff_refused") from None
    settings = configuration.observation
    require(
        configuration.mode == "observation"
        and settings is not None
        and settings.read_schemas is not None
        and settings.legacy_identity_workload is not None
        and (not settings.write_enabled or settings.reconciler_role_arn is not None)
    )
    return configuration


def digest(value):
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    )


def restored(plan):
    result = copy.deepcopy(plan["before"])
    result["release"] = {
        **(result["release"] or plan["after"]["release"]),
        "group_generation": plan["after"]["release"]["group_generation"] + 1,
        "backend_generation": plan["after"]["release"]["backend_generation"] + 1,
    }
    return result


def execute(config, backend, transition, *, rollback=False, clock=time.time, sleep=time.sleep):
    plan = config["plan"]
    state = transition(action="read", plan=plan)
    if state is not None and state.get("transition", {}).get("plan") == plan:
        phase = state["transition"]["phase"]
        if phase == "restored" or phase == "active" and not rollback:
            expected = restored(plan) if phase == "restored" else plan["after"]
            require(backend.observe(plan["stage"], expected) == expected)
            backend.verify_configuration(expected["release"], active=phase == "active")
            return state
    backend.verify_prepared(plan)
    if state is None or state.get("transition", {}).get("plan") != plan:
        require(not rollback)
        require(backend.observe(plan["stage"], plan["before"]) == plan["before"])
        state = transition(action="fence", plan=plan)
    require(state is not None and state["transition"]["plan"] == plan)
    rollback = rollback or state["transition"]["phase"] == "rollback"

    def apply(restore):
        nonlocal state
        expected = restored(plan) if restore else plan["after"]
        if restore:
            state = transition(action="rollback", plan=plan)
        wait = state["transition"]["drain_until"] - clock()
        require(wait <= 21)
        if wait > 0:
            sleep(wait)
        backend.configure_release(
            expected["release"], configure_adapter=not (restore and expected["status"] == "legacy")
        )
        backend.switch(plan, expected["deployment_id"])
        observed = backend.observe(plan["stage"], expected)
        require(observed == expected)
        state = transition(action="restore" if restore else "activate", plan=plan, observed=observed)
        require(state["status"] == expected["status"] and state["release"] == expected["release"])
        return state

    try:
        return apply(rollback)
    except (
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        guard_release.GuardRefused,
        BotoCoreError,
        ClientError,
    ):
        if not rollback:
            apply(True)
        raise


class Backend:
    def __init__(self, config, environment, *, clients=None, run=subprocess.run):
        require(set(config) == {"plan", "selectors"})
        self.selectors, self.env, self.run = config["selectors"], environment, run
        self.plan = config["plan"]
        require(
            set(self.selectors) - {"runtime_before"}
            == {
                "account_id",
                "region",
                "gateway_id",
                "candidate_stage",
                "baseline_stage",
                "legacy_host",
                "adapter_host",
                "workloads",
                "adapter_namespace",
            }
        )
        require(self.selectors["account_id"] == environment["ADP_TOOLS_ACCOUNT_ID"])
        require(self.selectors["region"] == environment["ADP_TOOLS_AWS_REGION"])
        for field in ("gateway_id", "candidate_stage", "baseline_stage", "adapter_namespace"):
            require(
                isinstance(self.selectors[field], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.selectors[field])
            )
        require(len({self.plan["stage"], self.selectors["candidate_stage"], self.selectors["baseline_stage"]}) == 3)
        for field in ("legacy_host", "adapter_host"):
            require(re.fullmatch(r"internal-[A-Za-z0-9.-]+\.elb\.amazonaws\.com", self.selectors[field]))
        require(self.selectors["legacy_host"] != self.selectors["adapter_host"])
        require(type(self.selectors["workloads"]) is dict and set(self.selectors["workloads"]) == {"gateway", "facade"})
        for selected in self.selectors["workloads"].values():
            require(type(selected) is dict and set(selected) == {"namespace", "deployment", "container"})
            require(
                all(
                    isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value)
                    for value in selected.values()
                )
            )
        factory = clients or (lambda service: boto3.client(service, region_name=self.selectors["region"]))
        self.api, self.targets = factory("apigateway"), factory("bedrock-agentcore-control")
        identity = factory("sts").get_caller_identity()
        role = environment["ADP_TOOLS_DEPLOY_ROLE_ARN"]
        account, name = role.split(":")[4], role.rsplit("/", 1)[-1]
        require(identity.get("Account") == account == self.selectors["account_id"])
        require(re.fullmatch(rf"arn:aws:sts::{account}:assumed-role/{re.escape(name)}/[^/]+", identity.get("Arn", "")))
        cluster = factory("eks").describe_cluster(name=environment["ADP_TOOLS_EKS_CLUSTER_NAME"])["cluster"]
        require(
            cluster["arn"]
            == f"arn:aws:eks:{self.selectors['region']}:{account}:cluster/{environment['ADP_TOOLS_EKS_CLUSTER_NAME']}"
            and cluster["status"] == "ACTIVE"
        )
        kube = json.loads(self.kubectl("config", "view", "--minify", "--raw", "-o", "json"))
        require(
            len(kube["clusters"]) == 1
            and kube["clusters"][0]["cluster"].get("server") == cluster["endpoint"]
            and kube["clusters"][0]["cluster"].get("certificate-authority-data")
            == cluster["certificateAuthority"]["data"]
            and not kube["clusters"][0]["cluster"].get("insecure-skip-tls-verify", False)
        )

    def guard(self):
        guard_release.authorize(self.env)

    def export(self, stage):
        selected = self.api.get_stage(restApiId=self.plan["api_id"], stageName=stage)
        require(not selected.get("canarySettings", {}).get("percentTraffic", 0))
        stream = self.api.get_export(
            restApiId=self.plan["api_id"],
            stageName=stage,
            exportType="swagger",
            parameters={"extensions": "integrations,authorizers"},
        )["body"]
        try:
            raw = stream.read(MAX_DOCUMENT + 1)
        finally:
            stream.close()
        require(len(raw) <= MAX_DOCUMENT)
        document = json.loads(raw)
        require(type(document) is dict and type(document.get("paths")) is dict)
        confirmed = self.api.get_stage(restApiId=self.plan["api_id"], stageName=stage)
        require(stage_configuration(confirmed) == stage_configuration(selected))
        return selected["deploymentId"], document

    def observe(self, stage, expected):
        deployment, document = self.export(stage)
        require(deployment == expected["deployment_id"])
        routes = {}
        for connector in sorted(CONNECTORS):
            path = "/internal/v1/tools/" + connector + "/mcp"
            target_id = expected["routes"][connector]["target_id"]
            target = self.targets.get_gateway_target(gatewayIdentifier=self.selectors["gateway_id"], targetId=target_id)
            require(
                target.get("targetId") == target_id
                and target.get("name") == connector
                and target.get("status") == "READY"
            )
            integration = document["paths"].get(path)
            mode = expected["routes"][connector]["backend"]
            if mode == "disabled":
                require(integration is None and connector == "atlassian")
            else:
                require(type(integration) is dict and set(integration) == {"post"})
                method = integration["post"]
                route = method["x-amazon-apigateway-integration"]
                uri = urlsplit(route["uri"])
                require(
                    uri.scheme == "http"
                    and uri.hostname == self.selectors[mode + "_host"]
                    and uri.path == path
                    and not uri.username
                    and not uri.password
                    and not uri.query
                    and not uri.fragment
                )
                require(
                    route.get("connectionType") == "VPC_LINK"
                    and route.get("httpMethod") == "POST"
                    and route.get("type") == "http_proxy"
                    and method.get("security") == [{"sigv4": []}]
                )
                require(
                    document.get("securityDefinitions", {}).get("sigv4", {}).get("x-amazon-apigateway-authtype")
                    == "awsSigv4"
                )
            projection = {
                key: target.get(key)
                for key in (
                    "targetId",
                    "name",
                    "targetConfiguration",
                    "credentialProviderConfigurations",
                    "privateEndpoint",
                )
            }
            routes[connector] = {
                "backend": mode,
                "target_id": target_id,
                "digest": digest({"route": integration, "target": projection}),
            }
        return {**expected, "routes": routes, "deployment_id": deployment}

    def verify_prepared(self, plan):
        # A plan prepared before catalogue snapshots existed cannot be safely
        # restored by this writer. Refuse before fencing or mutating anything.
        saved = self.selectors.get("runtime_before")
        require(type(saved) is dict and set(saved) == {"maps", "workloads"})
        require(set(saved["maps"]) == {value["namespace"] for value in self.selectors["workloads"].values()})
        require(set(saved["workloads"]) == set(self.selectors["workloads"]))
        previous = plan["before"]["release"]
        if previous is not None and plan["before"]["status"] == "active":
            require(
                all(previous[field] == plan["after"]["release"][field] for field in ("source_revision", "image_digest"))
            )
        for stage, expected in (
            (self.selectors["baseline_stage"], plan["before"]),
            (self.selectors["candidate_stage"], plan["after"]),
        ):
            require(self.observe(stage, expected) == expected)
        _, before = self.export(self.selectors["baseline_stage"])
        _, after = self.export(self.selectors["candidate_stage"])
        for document in (before, after):
            document.pop("basePath", None)
            for connector in CONNECTORS:
                document["paths"].pop("/internal/v1/tools/" + connector + "/mcp", None)
        require(before == after)

    def kubectl(self, *args, payload=None, mutation=False):
        if mutation:
            self.guard()
        completed = self.run(
            ["kubectl", "--request-timeout=30s", *args], input=payload, capture_output=True, text=True, timeout=330
        )
        require(completed.returncode == 0 and len(completed.stdout) <= MAX_DOCUMENT)
        return completed.stdout

    def adapter_contract(self, release):
        """Read and fully validate the deployed contract before any mutation."""
        namespace = self.selectors["adapter_namespace"]
        current = json.loads(self.kubectl("-n", namespace, "get", "configmap/tool-adapters-contract", "-o", "json"))
        configuration = connector_configuration(json.loads(current["data"]["config.json"]))
        deployed = configuration.release.model_dump(mode="json")
        # The deployed image must be exactly the configured one. Only the
        # release group may change, and only away from the foundation group.
        require(all(deployed[key] == release[key] for key in ("source_revision", "image_digest")))
        require(release["group"] != FOUNDATION_GROUP)
        return current

    def configure_release(self, release, *, configure_adapter=True):
        serialized = json.dumps(release, sort_keys=True, separators=(",", ":"))
        # The legacy connectors may reach the adapter only while this fenced
        # path runs. The restore path configures the flag false and returns
        # legacy routing, so a rollback is always retained.
        flag = "true" if configure_adapter else "false"
        desired = {"ADP_TOOL_ADAPTER_RELEASE": serialized, "ADP_TOOL_ADAPTER_LEGACY_ENABLED": flag}
        current = self.adapter_contract(release) if configure_adapter else None
        configuration = connector_configuration(json.loads(current["data"]["config.json"])) if current else None
        catalogue_binding.install(self, release, configuration)
        for selected in self.selectors["workloads"].values():
            self.kubectl(
                "-n",
                selected["namespace"],
                "set",
                "env",
                "deployment/" + selected["deployment"],
                "--containers=" + selected["container"],
                "ADP_TOOL_ADAPTER_RELEASE=" + serialized,
                "ADP_TOOL_ADAPTER_LEGACY_ENABLED=" + flag,
                mutation=True,
            )
            self.kubectl(
                "-n",
                selected["namespace"],
                "rollout",
                "status",
                "deployment/" + selected["deployment"],
                "--timeout=300s",
            )
            observed = json.loads(
                self.kubectl("-n", selected["namespace"], "get", "deployment/" + selected["deployment"], "-o", "json")
            )
            containers = [
                container
                for container in observed["spec"]["template"]["spec"]["containers"]
                if container["name"] == selected["container"]
            ]
            require(
                len(containers) == 1
                and {
                    value["name"]: value["value"] for value in containers[0].get("env", []) if value["name"] in desired
                }
                == desired
            )
        if not configure_adapter:
            catalogue_binding.verify(self, release)
            return
        namespace = self.selectors["adapter_namespace"]
        settings = json.loads(current["data"]["config.json"])
        settings["release"] = release
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/data/config.json", "value": json.dumps(settings, sort_keys=True)},
        ]
        self.kubectl(
            "-n",
            namespace,
            "patch",
            "configmap/tool-adapters-contract",
            "--type=json",
            # Name the same field manager the adapter release itself applies
            # with. Without it kubectl patch records the default kubectl-patch
            # manager for /data/config.json, and the next server-side apply of
            # the replacement container is refused with a conflict on that
            # field. Only this field's ownership is affected.
            "--field-manager=adp-tool-adapters",
            "--patch=" + json.dumps(patch),
            mutation=True,
        )
        self.kubectl(
            "-n",
            namespace,
            "set",
            "env",
            "deployment/tool-adapters",
            "--containers=tool-adapters",
            "ADP_TOOL_ADAPTER_LEGACY_ENABLED=true",
            mutation=True,
        )
        self.kubectl("-n", namespace, "rollout", "restart", "deployment/tool-adapters", mutation=True)
        self.kubectl("-n", namespace, "rollout", "status", "deployment/tool-adapters", "--timeout=300s")
        catalogue_binding.verify(self, release, configuration)

    def verify_configuration(self, release, *, active):
        current = self.adapter_contract(release) if active else None
        configuration = connector_configuration(json.loads(current["data"]["config.json"])) if current else None
        catalogue_binding.verify(self, release, configuration)

    def switch(self, plan, deployment):
        self.guard()
        current = self.api.get_stage(restApiId=plan["api_id"], stageName=plan["stage"])
        require(current["deploymentId"] in {plan["before"]["deployment_id"], plan["after"]["deployment_id"]})
        require(not current.get("canarySettings", {}).get("percentTraffic", 0))
        if current["deploymentId"] != deployment:
            self.api.update_stage(
                restApiId=plan["api_id"],
                stageName=plan["stage"],
                patchOperations=[{"op": "replace", "path": "/deploymentId", "value": deployment}],
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("handoff", "rollback"))
    args = parser.parse_args()
    try:
        path = Path(os.environ["ADP_TOOLS_ADAPTER_HANDOFF_FILE"])
        require(
            path.is_file()
            and not path.is_symlink()
            and not path.stat().st_mode & 0o077
            and path.stat().st_size <= MAX_DOCUMENT
        )
        config = json.loads(path.read_bytes())
        guard_release.authorize(os.environ)
        backend = Backend(config, os.environ)
        result = execute(
            config,
            backend,
            lambda **value: guard_release.authorize(os.environ, transition=value),
            rollback=args.action == "rollback",
        )
        require(result["transition"]["phase"] in {"active", "restored"})
        expected = restored(config["plan"]) if args.action == "rollback" else config["plan"]["after"]
        observed = backend.observe(config["plan"]["stage"], expected)
        require(observed == expected)
        from prepare_handoff import private_write

        private_write(
            os.environ["ADP_TOOLS_ADAPTER_HANDOFF_RESULT"], {"config": config, "state": result, "observed": observed}
        )
    except Exception:
        raise SystemExit(
            "Adapter handoff did not complete; inspect the retained core transition before retrying"
        ) from None
    print("Adapter transition completed with verified four-route readback")


if __name__ == "__main__":
    main()
