"""Install handoff-owned catalogue settings without changing foundation config.

The platform creates the empty ConfigMap and grants named patch access. Only the
fenced handoff writes its data. Both normal workload manifests consume it, so a
later application deployment cannot reset the active connector release.
"""

import copy
import hashlib
import json

NAME = "adp-tool-adapter-runtime"
CONNECTORS = ("atlassian", "github", "jira", "slack")
SUFFIXES = ("MEDIATOR_ENABLED", "MEDIATOR_URL", "GATEWAY_ID", "TARGET_ID", "SOURCE_REVISION")
CATALOGUE_FIELDS = {
    *(f"ADP_TOOLS_{name.upper()}_{suffix}" for name in CONNECTORS for suffix in SUFFIXES),
    "ADP_TOOLS_ATLASSIAN_READS_SHA256",
    "ADP_TOOLS_ATLASSIAN_WRITES_SHA256",
    "ADP_TOOLS_ATLASSIAN_WRITES_ENABLED",
    "ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED",
}
RELEASE_FIELDS = {"ADP_TOOL_ADAPTER_RELEASE", "ADP_TOOL_ADAPTER_LEGACY_ENABLED"}


def require(condition):
    if not condition:
        raise ValueError("adapter_catalogue_binding_refused")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def read(backend, namespace, resource):
    return json.loads(backend.kubectl("-n", namespace, "get", resource, "-o", "json"))


def container(document, name):
    selected = [item for item in document["spec"]["template"]["spec"]["containers"] if item["name"] == name]
    require(len(selected) == 1)
    return selected[0]


def snapshot(backend):
    """Record original data and references before any handoff mutation."""
    maps, workloads = {}, {}
    for name, selected in backend.selectors["workloads"].items():
        namespace = selected["namespace"]
        if namespace not in maps:
            document = read(backend, namespace, "configmap/" + NAME)
            values = document.get("data", {})
            require(set(values) <= CATALOGUE_FIELDS | RELEASE_FIELDS)
            maps[namespace] = values
        document = read(backend, namespace, "deployment/" + selected["deployment"])
        current = container(document, selected["container"])
        workloads[name] = {
            "env": [value for value in current.get("env", []) if value["name"] in CATALOGUE_FIELDS],
            "envFrom": current.get("envFrom", []),
        }
    return {"maps": maps, "workloads": workloads}


def desired(backend, release, configuration):
    """Derive bindings from the actual prepared targets and validated schemas."""
    settings = configuration.observation
    result = {
        "ADP_TOOL_ADAPTER_RELEASE": canonical(release),
        "ADP_TOOL_ADAPTER_LEGACY_ENABLED": "true",
        "ADP_TOOLS_ATLASSIAN_WRITES_ENABLED": str(settings.write_enabled).lower(),
        "ADP_TOOLS_ATLASSIAN_WRITES_SHA256": "",
        "ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED": str(getattr(settings, "system_context_enabled", False)).lower(),
    }
    for name in CONNECTORS:
        prefix = f"ADP_TOOLS_{name.upper()}_"
        result.update(
            {
                prefix + "MEDIATOR_ENABLED": "true",
                prefix + "MEDIATOR_URL": (
                    f"https://{backend.plan['api_id']}.execute-api.{backend.selectors['region']}.amazonaws.com/"
                    f"{backend.plan['stage']}/internal/v1/tools/{name}/mcp"
                ),
                prefix + "GATEWAY_ID": backend.selectors["gateway_id"],
                prefix + "TARGET_ID": backend.plan["after"]["routes"][name]["target_id"],
                prefix + "SOURCE_REVISION": release["source_revision"],
            }
        )
    schemas = dict(settings.read_schemas)
    tools = [{"name": alias, "description": alias, "inputSchema": schemas[alias]} for alias in sorted(schemas)]
    if getattr(settings, "system_context_enabled", False):
        from adp_tools.jira_context import context_tool

        tools = sorted([*tools, context_tool()], key=lambda item: item["name"])
    result["ADP_TOOLS_ATLASSIAN_READS_SHA256"] = hashlib.sha256(canonical(tools).encode()).hexdigest()
    if settings.write_enabled:
        schemas.update(settings.write_schemas)
        tools = [{"name": alias, "description": alias, "inputSchema": schemas[alias]} for alias in sorted(schemas)]
        if getattr(settings, "system_context_enabled", False):
            tools = sorted([*tools, context_tool()], key=lambda item: item["name"])
        result["ADP_TOOLS_ATLASSIAN_WRITES_SHA256"] = hashlib.sha256(canonical(tools).encode()).hexdigest()
    return result


def patch(backend, namespace, resource, current, changes):
    operations = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
        *changes,
    ]
    backend.kubectl(
        "-n", namespace, "patch", resource, "--type=json", "--patch=" + canonical(operations), mutation=True
    )


def install(backend, release, configuration=None):
    """Update configuration while fenced; restore never needs the adapter alive."""
    before = backend.selectors["runtime_before"]
    require(set(before) == {"maps", "workloads"})
    require(set(before["workloads"]) == set(backend.selectors["workloads"]))
    namespaces = {value["namespace"] for value in backend.selectors["workloads"].values()}
    require(set(before["maps"]) == namespaces)
    active = desired(backend, release, configuration) if configuration is not None else None
    for namespace in sorted(namespaces):
        values = copy.deepcopy(active if active is not None else before["maps"][namespace])
        require(set(values) <= CATALOGUE_FIELDS | RELEASE_FIELDS)
        # Generations must advance on rollback to invalidate old operations.
        values.update(
            ADP_TOOL_ADAPTER_RELEASE=canonical(release),
            ADP_TOOL_ADAPTER_LEGACY_ENABLED="true" if active is not None else "false",
        )
        document = read(backend, namespace, "configmap/" + NAME)
        patch(backend, namespace, "configmap/" + NAME, document, [{"op": "add", "path": "/data", "value": values}])
        require(read(backend, namespace, "configmap/" + NAME).get("data", {}) == values)
    for name, selected in backend.selectors["workloads"].items():
        namespace, resource = selected["namespace"], "deployment/" + selected["deployment"]
        document = read(backend, namespace, resource)
        current = container(document, selected["container"])
        index = document["spec"]["template"]["spec"]["containers"].index(current)
        original = before["workloads"][name]
        require(set(original) == {"env", "envFrom"})
        require(all(value["name"] in CATALOGUE_FIELDS for value in original["env"]))
        values = [value for value in current.get("env", []) if value["name"] not in CATALOGUE_FIELDS]
        # Another component may add an unrelated source after preparation.
        # Own only our ConfigMap reference, not the workload's entire envFrom.
        sources = [
            copy.deepcopy(value)
            for value in current.get("envFrom", [])
            if value.get("configMapRef", {}).get("name") != NAME
        ]
        if active is not None:
            sources.append({"configMapRef": {"name": NAME, "optional": True}})
        else:
            values.extend(original["env"])
            for position, value in enumerate(original["envFrom"]):
                if value.get("configMapRef", {}).get("name") == NAME:
                    sources.insert(min(position, len(sources)), copy.deepcopy(value))
        prefix = f"/spec/template/spec/containers/{index}/"
        annotations = dict(document["spec"]["template"].get("metadata", {}).get("annotations", {}))
        annotations["adp.io/tool-adapter-runtime"] = hashlib.sha256(
            canonical(read(backend, namespace, "configmap/" + NAME).get("data", {})).encode()
        ).hexdigest()
        patch(
            backend,
            namespace,
            resource,
            document,
            [
                {"op": "add", "path": prefix + "env", "value": values},
                {"op": "add", "path": prefix + "envFrom", "value": sources},
                {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations},
            ],
        )
        observed = container(read(backend, namespace, resource), selected["container"])
        require(observed.get("env", []) == values and observed.get("envFrom", []) == sources)


def verify(backend, release, configuration=None):
    """Read current configuration again, including on an idempotent retry."""
    active = desired(backend, release, configuration) if configuration is not None else None
    before = backend.selectors["runtime_before"]
    for name, selected in backend.selectors["workloads"].items():
        namespace = selected["namespace"]
        values = copy.deepcopy(active if active is not None else before["maps"][namespace])
        values.update(
            ADP_TOOL_ADAPTER_RELEASE=canonical(release),
            ADP_TOOL_ADAPTER_LEGACY_ENABLED="true" if active is not None else "false",
        )
        require(read(backend, namespace, "configmap/" + NAME).get("data", {}) == values)
        current = container(read(backend, namespace, "deployment/" + selected["deployment"]), selected["container"])
        for value in current.get("env", []):
            if value["name"] in RELEASE_FIELDS:
                require(value.get("value") == values[value["name"]])
        overrides = [value for value in current.get("env", []) if value["name"] in CATALOGUE_FIELDS]
        if active is None:
            require(overrides == before["workloads"][name]["env"])
            require(
                [value for value in current.get("envFrom", []) if value.get("configMapRef", {}).get("name") == NAME]
                == [
                    value
                    for value in before["workloads"][name]["envFrom"]
                    if value.get("configMapRef", {}).get("name") == NAME
                ]
            )
        else:
            require(current.get("envFrom", [])[-1:] == [{"configMapRef": {"name": NAME, "optional": True}}])
            for value in overrides:
                require(
                    value.get("value") == values[value["name"]]
                    or value.get("valueFrom", {}).get("configMapKeyRef")
                    == {"name": NAME, "key": value["name"], "optional": True}
                )
