#!/usr/bin/env python3
"""Operator-only live isolation probes. Never assigns qualification labels.

Run with the private deployment identity and an immutable Python-capable image.
Failed or interrupted cleanup retains finalizers and records unknown termination.
"""
import argparse
import datetime
import json
import pathlib
import subprocess
import time
import uuid

FINALIZER = "adp.dev/validation-qualification-evidence"


def kubectl(*args, body=None):
    result = subprocess.run(
        ["kubectl", "--request-timeout=30s", *args],
        input=json.dumps(body) if body is not None else None,
        text=True, capture_output=True, timeout=40,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def get(namespace, name):
    return json.loads(kubectl("get", "pod", name, "-n", namespace, "-o", "json"))


def until(callback, seconds=300):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = callback()
        if value:
            return value
        time.sleep(2)
    raise TimeoutError("qualification observation deadline exceeded")


def cleanup(namespace, name, uid):
    pod = get(namespace, name)
    if pod["metadata"]["uid"] != uid:
        raise RuntimeError("Pod UID changed; retaining evidence")
    kubectl("delete", "pod", name, "-n", namespace, "--wait=false")

    def terminated():
        pod = get(namespace, name)
        if pod["metadata"]["uid"] != uid:
            raise RuntimeError("Pod UID changed during cleanup")
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if statuses and all("terminated" in s.get("state", {}) for s in statuses):
            return {"uid": uid, "containers": statuses, "observedTermination": True}
        if not pod.get("spec", {}).get("nodeName") and not statuses:
            return {"uid": uid, "neverScheduled": True, "observedTermination": True}

    receipt = until(terminated, 90)
    kubectl("patch", "pod", name, "-n", namespace, "--type=json", "-p", json.dumps([
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "replace", "path": "/metadata/finalizers", "value": []},
    ]))
    return receipt


def pod(namespace, name, image, program, run_id):
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"adp.dev/qualification": run_id}, "finalizers": [FINALIZER]},
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False, "restartPolicy": "Never",
            "activeDeadlineSeconds": 600, "terminationGracePeriodSeconds": 5,
            "nodeSelector": {"adp.dev/validation-candidate": "v1"},
            "tolerations": [{"key": "adp.dev/validation", "operator": "Equal", "value": "only", "effect": "NoSchedule"}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "probe", "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["/var/lang/bin/python3.12", "-u", "-c", program],
                "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
        },
    }


CONTROL = '''
import json,socket,http.server
with socket.create_connection(("1.1.1.1",443),timeout=5): pass
print(json.dumps({"reachableControl":True}),flush=True)
http.server.HTTPServer(("0.0.0.0",8080),http.server.BaseHTTPRequestHandler).serve_forever()
'''

PROBE = '''
import errno,json,os,socket,time,http.server
assert os.getuid()==65534
assert not os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
for key in ["AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","AWS_SESSION_TOKEN","AWS_WEB_IDENTITY_TOKEN_FILE","AWS_CONTAINER_CREDENTIALS_FULL_URI","AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"]:
    assert key not in os.environ,key
status=dict(line.split(":",1) for line in open("/proc/self/status") if ":" in line)
assert int(status["CapEff"].strip(),16)==0
assert status["Seccomp"].strip()=="2"
try:
    open("/qualification-root-write", "w")
except OSError as e:
    assert e.errno==errno.EROFS,e
else: raise AssertionError("root writable")
for host,port in [("1.1.1.1",443),("169.254.169.254",80),("169.254.170.23",80)]:
    try:
        socket.create_connection((host,port),timeout=3).close()
    except (TimeoutError,OSError): pass
    else: raise AssertionError("network allowed: "+host)
children=[]
bounded=False
try:
    for i in range(140):
        try: pid=os.fork()
        except OSError as e:
            assert e.errno==errno.EAGAIN,e
            bounded=True
            break
        if pid==0:
            time.sleep(30)
            os._exit(0)
        children.append(pid)
finally:
    for pid in children: os.kill(pid,9)
    for pid in children: os.waitpid(pid,0)
assert bounded and 0<len(children)<128,len(children)
print(json.dumps({"isolationPassed":True,"spawnedChildren":len(children),"credentialFree":True,"readOnlyRoot":True,"networkDenied":True,"seccomp":2,"effectiveCapabilities":0}),flush=True)
http.server.HTTPServer(("0.0.0.0",8080),http.server.BaseHTTPRequestHandler).serve_forever()
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--promote", action="store_true", help="Label only the successfully qualified node UID and retain it for this rollout")
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        parser.error("immutable registry image required")
    run_id = "q-" + uuid.uuid4().hex[:12]
    control_ns = "adp-validation-control-" + run_id
    evidence = {"runId": run_id, "image": args.image, "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "passed": False, "cleanup": []}
    created = []
    try:
        kubectl("create", "-f", "-", body={"apiVersion": "v1", "kind": "Namespace", "metadata": {
            "name": control_ns, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
        kubectl("create", "-f", "-", body={"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "reachable-control", "namespace": control_ns},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [{}], "egress": [{}]}})
        for ns, name, program in [(control_ns, "control", CONTROL), ("adp-codex-validation", run_id, PROBE)]:
            # Record intent first. An uncertain create is retained for reconciliation.
            created.append((ns, name, None))
            response = json.loads(kubectl("create", "-f", "-", "-o", "json", body=pod(ns, name, args.image, program, run_id)))
            created[-1] = (ns, name, response["metadata"]["uid"])
        for ns, name, _ in created:
            until(lambda: get(ns, name).get("status", {}).get("phase") == "Running")
        control = get(control_ns, "control")
        target = get("adp-codex-validation", run_id)
        assert control["spec"]["nodeName"] == target["spec"]["nodeName"], "controls must use the same node"
        node = target["spec"]["nodeName"]
        config = json.loads(kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/configz"))["kubeletconfig"]
        assert 0 < config["podPidsLimit"] <= 128, config["podPidsLimit"]
        evidence.update(node=node, nodeUid=json.loads(kubectl("get", "node", node, "-o", "json"))["metadata"]["uid"], podPidsLimit=config["podPidsLimit"])
        for ns, name, marker in [(control_ns, "control", '"reachableControl": true'), ("adp-codex-validation", run_id, '"isolationPassed": true')]:
            logs = until(lambda: (text if marker in (text := kubectl("logs", name, "-n", ns)) else None), 60)
            evidence[name] = json.loads(logs.splitlines()[0])
        destination = target["status"]["podIP"]
        control_ip = control["status"]["podIP"]
        ingress = f'''import socket,json
with socket.create_connection(({control_ip!r},8080),timeout=3): pass
try: socket.create_connection(({destination!r},8080),timeout=3).close()
except (TimeoutError,OSError): print(json.dumps({{"ingressDenied":True}}))
else: raise AssertionError("ingress allowed")
'''
        evidence["ingress"] = json.loads(kubectl("exec", "-n", control_ns, "control", "--", "/var/lang/bin/python3.12", "-c", ingress))
        evidence["probesPassed"] = True
    except Exception as exc:
        evidence["error"] = str(exc)
    finally:
        for ns, name, uid in created:
            try:
                if uid is None:
                    raise RuntimeError("uncertain create; inspect Pod before reconciliation")
                receipt = cleanup(ns, name, uid)
                evidence["cleanup"].append({"namespace": ns, "name": name, **receipt})
            except Exception as exc:
                evidence["cleanup"].append({"namespace": ns, "name": name, "unknown": str(exc)})
        clean = len(created) == 2 and all(r.get("observedTermination") for r in evidence["cleanup"])
        evidence["passed"] = bool(evidence.get("probesPassed") and clean)
        if evidence["passed"] and args.promote:
            try:
                current = json.loads(kubectl("get", "node", evidence["node"], "-o", "json"))
                assert current["metadata"]["uid"] == evidence["nodeUid"], "qualified node was replaced"
                assert not current["metadata"].get("deletionTimestamp"), "qualified node is terminating"
                kubectl("patch", "node", evidence["node"], "--type=json", "-p", json.dumps([
                    {"op": "test", "path": "/metadata/uid", "value": evidence["nodeUid"]},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                    {"op": "add", "path": "/metadata/labels/adp.dev~1validation-isolation", "value": "v1"},
                    {"op": "add", "path": "/metadata/annotations/karpenter.sh~1do-not-disrupt", "value": "true"},
                    {"op": "add", "path": "/metadata/annotations/adp.dev~1validation-qualification", "value": run_id},
                ]))
                evidence["promoted"] = True
            except Exception as exc:
                evidence["passed"] = False
                evidence["promotionError"] = str(exc)
        pathlib.Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
        if clean:
            kubectl("delete", "namespace", control_ns, "--wait=false")
        print(json.dumps({"runId": run_id, "passed": evidence["passed"], "error": evidence.get("error")}))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
