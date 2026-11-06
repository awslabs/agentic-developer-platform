"""Check paired scope denials and isolated-Pod observations without claiming live qualification."""

from collections.abc import Mapping


SURFACES = {"client": "gateway", "broker": "broker", "source": "source-api"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _trace(trace, *, actor, reference, source, allowed):
    _require(isinstance(trace, Mapping), "boundary trace is missing")
    _require(trace.get("actor") == actor and trace.get("reference") == reference
             and trace.get("source") == source and isinstance(trace.get("evidence_ref"), str)
             and bool(trace["evidence_ref"]), "boundary trace is not attributable")
    if allowed:
        _require(trace.get("status") == 200 and trace.get("resource_id") == reference["record_id"],
                 "owned resource positive control failed")
    else:
        _require(trace.get("status") in {403, 404}
                 and trace.get("body") in (None, {}, {"detail": {"code": "not_found"}})
                 and trace.get("effect_count") == 0,
                 "foreign boundary disclosed data or caused an effect")
    return trace["evidence_ref"]


def _pod(observation, scope):
    _require(isinstance(observation, Mapping) and observation.get("source") == "kubernetes-api"
             and observation.get("scope") == scope
             and isinstance(observation.get("evidence_ref"), str) and bool(observation["evidence_ref"]),
             "executor observation is not attributable")
    pod = observation.get("pod")
    _require(isinstance(pod, Mapping), "executor Pod is missing")
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    _require(isinstance(metadata, Mapping) and isinstance(spec, Mapping),
             "executor Pod identity or host boundary is unsafe")
    containers = spec.get("containers", [])
    volumes = spec.get("volumes", [])
    _require(bool(metadata.get("uid"))
             and spec.get("automountServiceAccountToken") is False
             and spec.get("enableServiceLinks") is False
             and spec.get("hostNetwork") is False and spec.get("hostPID") is False
             and spec.get("hostIPC") is False
             and isinstance(volumes, list)
             and all(isinstance(volume, Mapping) and (
                 set(volume) == {"name", "emptyDir"} or (
                     set(volume) == {"name", "projected"}
                     and isinstance(volume["projected"], Mapping)
                     and isinstance(volume["projected"].get("sources"), list)
                     and bool(volume["projected"]["sources"])
                     and all(isinstance(source, Mapping) and set(source) == {"configMap"}
                             for source in volume["projected"]["sources"]))) for volume in volumes)
             and isinstance(containers, list) and len(containers) == 1,
             "executor Pod identity or host boundary is unsafe")
    container = containers[0]
    _require(isinstance(container, Mapping), "executor container security boundary is unsafe")
    security = container.get("securityContext", {})
    environment = container.get("env", [])
    _require(isinstance(security, Mapping) and isinstance(environment, list)
             and sorted(environment, key=lambda item: item.get("name", "") if isinstance(item, Mapping) else "") == [
                 {"name": "BG_CONFIG_DIR", "value": "/tmp/bg"},
                 {"name": "HOME", "value": "/tmp"},
                 {"name": "TMPDIR", "value": "/tmp"},
             ] and not container.get("envFrom")
             and security.get("readOnlyRootFilesystem") is True
             and security.get("allowPrivilegeEscalation") is False
             and security.get("capabilities", {}).get("drop") == ["ALL"],
             "executor container security boundary is unsafe")
    probes = observation.get("probes", {})
    _require(isinstance(probes, Mapping) and set(probes) == {
        "allowed_source_read", "metadata_denied", "egress_denied",
        "credentials_denied", "source_escape_denied", "host_escape_denied",
    } and all(value is True for value in probes.values()),
             "executor isolation or source-access control is unproven")
    return observation["evidence_ref"]


async def probe_boundaries(driver, *, owner, foreign, reference):
    """Invoke read-only controls through the supplied gateway, broker, source and Pod driver."""
    _require(isinstance(owner, Mapping) and isinstance(foreign, Mapping)
             and isinstance(reference, Mapping)
             and set(owner) == {"tenant_id", "product_id", "project_id"}
             and set(foreign) == set(owner) and owner != foreign
             and reference.get("tenant_id") == owner["tenant_id"]
             and reference.get("project_id") == owner["project_id"]
             and isinstance(reference.get("record_id"), str) and bool(reference["record_id"]),
             "boundary fixture must pin separate authorized scopes and one owned resource")
    evidence = {}
    for surface, source in SURFACES.items():
        owned = await driver.read(surface, owner, reference)
        evidence[f"{surface}_owned"] = _trace(
            owned, actor=owner, reference=reference, source=source, allowed=True)
        denied = await driver.read(surface, foreign, reference)
        evidence[f"{surface}_foreign"] = _trace(
            denied, actor=foreign, reference=reference, source=source, allowed=False)
    evidence["executor"] = _pod(await driver.probe_executor(owner), owner)
    _require(len(set(evidence.values())) == len(evidence), "boundary probes reused an observation")
    return {"phase": "boundary-observations", "evidence_refs": evidence, "live_qualified": False}
