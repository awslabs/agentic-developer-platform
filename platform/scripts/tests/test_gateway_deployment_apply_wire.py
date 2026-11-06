"""Independent offline exercise of actual kubectl against a synthetic API.

The fake API supplies discovery, schema and compare-and-swap storage; kubectl
itself computes client-side apply's patch and last-applied annotation.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

HELPER = (
    Path(__file__).resolve().parents[3]
    / "modules/gateway/scripts/apply-gateway-deployment.py"
)
IMAGE = "example.test/gateway@sha256:" + "a" * 64
API_PATH = "/apis/apps/v1/namespaces/adp-gateway/deployments/bedrockgateway"
COLLECTION = API_PATH.rsplit("/", 1)[0]
ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"


def desired():
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "bedrockgateway",
            "namespace": "adp-gateway",
            "labels": {"app": "bedrockgateway"},
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "bedrockgateway"}},
            "template": {
                "metadata": {"labels": {"app": "bedrockgateway"}},
                "spec": {
                    "containers": [
                        {
                            "name": "bedrockgateway",
                            "image": "REPLACE_WITH_GATEWAY_IMAGE",
                        }
                    ]
                },
            },
        },
    }


def initial():
    last = desired()
    last["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "STALE_OWNED_FIELD", "value": "remove"}
    ]
    current = copy.deepcopy(last)
    current["metadata"].update(uid="gateway-uid", resourceVersion="41", generation=8)
    current["metadata"]["annotations"] = {
        ANNOTATION: json.dumps(last, separators=(",", ":")),
        "unrelated-controller": "retain",
    }
    current["spec"]["replicas"] = 17
    current["spec"]["revisionHistoryLimit"] = 9
    current["status"] = {"replicas": 17, "availableReplicas": 17}
    return current


def merge(current, patch):
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(current) if isinstance(current, dict) else {}
    for key, value in patch.items():
        if key.startswith("$"):
            continue
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = merge(result.get(key), value)
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(x, dict) and "name" in x for x in value)
        ):
            old = {x["name"]: x for x in result.get(key, [])}
            for item in value:
                if item.get("$patch") == "delete":
                    old.pop(item["name"], None)
                else:
                    old[item["name"]] = merge(old.get(item["name"]), item)
            result[key] = list(old.values())
        else:
            result[key] = copy.deepcopy(value)
    return result


def schema():
    # Minimal standard OpenAPI schema needed by kubectl's native Deployment
    # handling; the native strategic-merge schema computes list patch semantics.
    return {
        "swagger": "2.0",
        "info": {"title": "offline-api", "version": "v1"},
        "paths": {
            COLLECTION: {
                "post": {
                    "consumes": ["application/json"],
                    "parameters": [
                        {"name": "fieldValidation", "in": "query", "type": "string"}
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            API_PATH: {
                "patch": {
                    "consumes": ["application/strategic-merge-patch+json"],
                    "parameters": [
                        {"name": "fieldValidation", "in": "query", "type": "string"}
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
        "definitions": {
            "io.k8s.api.apps.v1.Deployment": {
                "type": "object",
                "x-kubernetes-group-version-kind": [
                    {"group": "apps", "version": "v1", "kind": "Deployment"}
                ],
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"type": "object"},
                    "spec": {"type": "object"},
                },
            }
        },
    }


class State:
    def __init__(self, mode):
        self.mode = mode
        self.object = None if mode in {"create", "create-race"} else initial()
        if mode == "missing-replicas":
            del self.object["spec"]["replicas"]
        self.requests = []
        self.mutations = []
        self.reads = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def status(self, code, reason):
        self.reply(
            code,
            {
                "apiVersion": "v1",
                "kind": "Status",
                "status": "Failure",
                "reason": reason,
                "message": reason,
                "details": {
                    "name": "bedrockgateway",
                    "group": "apps",
                    "kind": "deployments",
                },
                "code": code,
            },
        )

    def do_GET(self):
        state = self.server.state
        url = urlsplit(self.path)
        state.requests.append(
            {"method": "GET", "path": url.path, "query": parse_qs(url.query)}
        )
        if url.path == "/api":
            self.reply(
                200,
                {
                    "apiVersion": "v1",
                    "kind": "APIVersions",
                    "versions": ["v1"],
                    "serverAddressByClientCIDRs": [],
                },
            )
        elif url.path == "/apis":
            self.reply(
                200,
                {
                    "apiVersion": "v1",
                    "kind": "APIGroupList",
                    "groups": [
                        {
                            "name": "apps",
                            "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                            "preferredVersion": {
                                "groupVersion": "apps/v1",
                                "version": "v1",
                            },
                        }
                    ],
                },
            )
        elif url.path == "/api/v1":
            self.reply(
                200,
                {
                    "apiVersion": "v1",
                    "kind": "APIResourceList",
                    "groupVersion": "v1",
                    "resources": [],
                },
            )
        elif url.path == "/apis/apps/v1":
            self.reply(
                200,
                {
                    "apiVersion": "v1",
                    "kind": "APIResourceList",
                    "groupVersion": "apps/v1",
                    "resources": [
                        {
                            "name": "deployments",
                            "singularName": "deployment",
                            "namespaced": True,
                            "kind": "Deployment",
                            "verbs": ["get", "list", "create", "update", "patch"],
                            "shortNames": ["deploy"],
                        }
                    ],
                },
            )
        elif url.path == "/version":
            self.reply(200, {"major": "1", "minor": "35", "gitVersion": "v1.35.8"})
        elif url.path == "/openapi/v3":
            self.reply(
                200,
                {
                    "paths": {
                        "apis/apps/v1": {
                            "serverRelativeURL": "/openapi/v3/apis/apps/v1"
                        }
                    }
                },
            )
        elif url.path == "/openapi/v3/apis/apps/v1":
            v2 = schema()
            for item in v2["paths"].values():
                for operation in item.values():
                    operation["x-kubernetes-group-version-kind"] = {
                        "group": "apps",
                        "version": "v1",
                        "kind": "Deployment",
                    }
                    for parameter in operation["parameters"]:
                        parameter["schema"] = {"type": parameter.pop("type")}
            self.reply(
                200,
                {
                    "openapi": "3.0.0",
                    "info": v2["info"],
                    "paths": v2["paths"],
                    "components": {"schemas": v2["definitions"]},
                },
            )
        elif url.path == "/openapi/v2":
            self.status(404, "NotFound")
        elif url.path == API_PATH:
            state.reads += 1
            if state.mode == "forbidden":
                self.status(403, "Forbidden")
                return
            if state.mode == "dry-run-race" and state.reads == 2:
                state.object["metadata"]["resourceVersion"] = "42"
                state.object["spec"]["replicas"] = 18
            if state.object is None:
                self.status(404, "NotFound")
            else:
                self.reply(200, state.object)
        else:
            self.status(404, "NotFound")

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                if not size:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                assert self.rfile.read(2) == b"\r\n"
            return json.loads(b"".join(chunks))
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))

    def do_PATCH(self):
        state = self.server.state
        url = urlsplit(self.path)
        body = self.read_body()
        request = {
            "method": "PATCH",
            "path": url.path,
            "query": parse_qs(url.query),
            "body": body,
        }
        state.requests.append(request)
        if url.path != API_PATH or state.object is None:
            self.status(404, "NotFound")
            return
        if request["query"].get("dryRun") != ["All"]:
            state.mutations.append(request)
            self.status(400, "NonDryRunForbiddenInTest")
            return
        candidate = merge(state.object, body)
        self.reply(200, candidate)
        if state.mode == "replace-race":
            state.object["metadata"]["resourceVersion"] = "42"
            state.object["spec"]["replicas"] = 18
        elif state.mode == "recreate-race":
            state.object["metadata"]["uid"] = "replacement-uid"
            state.object["metadata"]["resourceVersion"] = "99"
            state.object["spec"]["replicas"] = 20

    def do_PUT(self):
        state = self.server.state
        url = urlsplit(self.path)
        body = self.read_body()
        request = {
            "method": "PUT",
            "path": url.path,
            "query": parse_qs(url.query),
            "body": body,
        }
        state.requests.append(request)
        if url.path != API_PATH:
            self.status(404, "NotFound")
            return
        if (
            body.get("metadata", {}).get("resourceVersion")
            != state.object["metadata"]["resourceVersion"]
            or body.get("metadata", {}).get("uid") != state.object["metadata"]["uid"]
        ):
            self.status(409, "Conflict")
            return
        state.object = body
        state.mutations.append(request)
        self.reply(200, state.object)

    def do_POST(self):
        state = self.server.state
        url = urlsplit(self.path)
        body = self.read_body()
        request = {
            "method": "POST",
            "path": url.path,
            "query": parse_qs(url.query),
            "body": body,
        }
        state.requests.append(request)
        if url.path != COLLECTION:
            self.status(404, "NotFound")
            return
        if state.mode == "create-race":
            state.object = initial()
            state.object["spec"]["replicas"] = 19
        if state.object is not None:
            self.status(409, "AlreadyExists")
            return
        state.object = body
        state.object["metadata"].update(uid="new-uid", resourceVersion="1")
        state.mutations.append(request)
        self.reply(201, state.object)


def run_case(mode):
    state = State(mode)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="adp-offline-kubectl-") as directory:
            base = Path(directory)
            config = base / "kubeconfig"
            config.write_text(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Config",
                        "clusters": [
                            {
                                "name": "offline",
                                "cluster": {
                                    "server": f"http://127.0.0.1:{server.server_port}"
                                },
                            }
                        ],
                        "contexts": [
                            {
                                "name": "offline",
                                "context": {"cluster": "offline", "user": "offline"},
                            }
                        ],
                        "current-context": "offline",
                        "users": [{"name": "offline", "user": {}}],
                    }
                )
            )
            manifest = base / "deployment.json"
            manifest.write_text(json.dumps(desired()))
            environ = {
                k: v for k, v in os.environ.items() if not k.startswith(("KUBE", "AWS"))
            }
            environ.update(KUBECONFIG=str(config), KUBECTL_KUBERC="false")
            result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER),
                    "--manifest",
                    str(manifest),
                    "--image",
                    IMAGE,
                ],
                env=environ,
                text=True,
                capture_output=True,
                timeout=45,
            )
            return state, result
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize(
    "mode",
    [
        "existing",
        "dry-run-race",
        "replace-race",
        "recreate-race",
        "create",
        "create-race",
        "forbidden",
        "missing-replicas",
    ],
)
def test_real_kubectl_pruning_annotation_and_write_fences(mode):
    assert shutil.which("kubectl"), (
        "Install kubectl to run the offline Kubernetes wire regression"
    )
    state, result = run_case(mode)
    if mode == "existing":
        assert result.returncode == 0, result.stderr
        assert len(state.mutations) == 1 and state.mutations[0]["method"] == "PUT"
        assert state.object["spec"]["replicas"] == 17
        assert state.object["spec"]["revisionHistoryLimit"] == 9
        assert (
            state.object["metadata"]["annotations"]["unrelated-controller"] == "retain"
        )
        (container,) = state.object["spec"]["template"]["spec"]["containers"]
        assert "env" not in container and container["image"] == IMAGE
        last = json.loads(state.object["metadata"]["annotations"][ANNOTATION])
        assert last["spec"]["replicas"] == 17
        assert not ({"uid", "resourceVersion"} & set(last["metadata"]))
        assert "unrelated-controller" not in last["metadata"].get("annotations", {})
        assert "revisionHistoryLimit" not in last["spec"]
        assert "status" not in state.mutations[0]["body"]
        assert state.mutations[0]["query"].get("fieldManager") == [
            "kubectl-client-side-apply"
        ]
    elif mode == "create":
        assert result.returncode == 0, result.stderr
        assert len(state.mutations) == 1 and state.mutations[0]["method"] == "POST"
        assert state.object["spec"]["replicas"] == 2
        assert ANNOTATION in state.object["metadata"]["annotations"]
    else:
        assert result.returncode != 0
        assert not state.mutations
        if mode in {"dry-run-race", "replace-race"}:
            assert state.object["spec"]["replicas"] == 18
        if mode == "recreate-race":
            assert state.object["spec"]["replicas"] == 20
        if mode == "create-race":
            assert state.object["spec"]["replicas"] == 19
