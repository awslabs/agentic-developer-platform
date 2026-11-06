#!/usr/bin/env python3
"""Resumable Superplane API demo. Python 3.10+, standard library only.

See docs/superplane-api-demo.md. The server remains the identity, approval,
ownership and runtime authority; this client never installs or bypasses them.
"""

import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class Stop(Exception):
    """Actionable stop with all request identities retained."""


class APIError(Stop):
    def __init__(self, status, method, path):
        self.status = status
        super().__init__(f"HTTP {status}: {method} {path}; state retained")


def now():
    return datetime.now(timezone.utc).isoformat()


def segment(value):
    return quote(str(value), safe="")


def base_url(value):
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise Stop("Use an HTTPS API base URL without credentials, query or fragment")
    return value.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop("API redirect refused; check the exact API base URL")


class API:
    def __init__(self, url, token_file):
        self.url = base_url(url)
        self.token_file = Path(token_file)
        self.opener = build_opener(NoRedirect())

    def token(self):
        if self.token_file.stat().st_mode & 0o077:
            raise Stop("Token file must be private (chmod 600)")
        token = self.token_file.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise Stop("Token file must contain one raw access token")
        return token

    def call(self, method, path, body=None):
        request = Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + self.token(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            # Never echo arbitrary upstream bodies, which may contain credentials.
            raise APIError(error.code, method, path) from None
        except (URLError, TimeoutError, OSError):
            raise Stop(
                f"Transport failed: {method} {path}; rerun with the same state"
            ) from None
        except ValueError:
            raise Stop("API returned invalid JSON; state retained") from None

    def identity(self):
        org = self.call("GET", "/orgs/current")["id"]
        # Local continuity hint only. The API verifies the signature, current
        # membership and permissions on every call, including every decision.
        try:
            encoded = self.token().split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
            if claims.get("token_use") != "access" or not claims.get("sub"):
                raise ValueError
            return {"org_id": org, "subject": claims["sub"]}
        except (ValueError, KeyError, IndexError):
            raise Stop("A genuine ADP human access token is required") from None


def write_json(path, value):
    """Atomic private write, including fsync before replacing durable state."""
    fd, temporary = tempfile.mkstemp(prefix=".demo-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_mode & 0o077:
            raise Stop("State directory must be private (chmod 700)")
        self.lock = (self.directory / "lock").open("a")
        os.chmod(self.directory / "lock", 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise Stop("Another demo process is using this state directory") from None
        self.path = self.directory / "state.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        self.data["updated_at"] = now()
        write_json(self.path, self.data)

    def evidence(self, label, value):
        name = f"{time.time_ns()}-{label}.json"
        write_json(self.directory / name, value)
        return name

    def close(self):
        self.lock.close()


def valid_ticket(ticket, request):
    # The persisted harness payload includes its version; workspace is top-level.
    expected = {
        "contract_version": "v1",
        **{k: request[k] for k in ("action", "idempotency_key", "parameters")},
    }
    if (
        ticket.get("workspace_id") != request["workspace_id"]
        or ticket.get("request") != expected
    ):
        raise Stop("Approval ticket does not match the exact reviewed request")


class Demo:
    def __init__(self, api, store, *, approve_own_plans=False):
        self.api, self.store, self.s = api, store, store.data
        self.approve_own_plans = approve_own_plans

    def bind(self):
        if not self.s or self.s.get("version") != 1:
            raise Stop("Initialize a new demo first")
        if (
            self.s["base_url"] != self.api.url
            or self.s["identity"] != self.api.identity()
        ):
            raise Stop(
                "Deployment, organization or requester changed; use the original scope"
            )

    def start_stage(self, kind, path, body, preview_path=None):
        self.s["pending"] = {
            "kind": kind,
            "path": path,
            "preview_body": body,
            "preview_path": preview_path or path + "/preview",
        }
        self.store.save()  # Persist identity before any network mutation.

    def stage(self):
        p = self.s["pending"]
        if "submission" not in p:
            review = self.api.call("POST", p["preview_path"], p["preview_body"])
            self.store.evidence("preview", review)
            if review.get("admission_available") is False:
                raise Stop(
                    "Runtime/retirement admission unavailable; inspect the saved preview"
                )
            request = review.get("approval_request")
            if not request or not re.fullmatch(
                r"[0-9a-f]{64}", review.get("revision", "")
            ):
                raise Stop("Preview has no valid approval request/revision")
            if p.get("review") and (
                p["review"]["revision"] != review["revision"]
                or p["review"]["approval_request"] != request
            ):
                raise Stop(
                    "Plan changed; saved approval cannot be reused. Inspect both previews"
                )
            p["review"] = review
            self.store.save()
            if "approval_id" not in p:
                ticket = self.api.call("POST", "/operation-approvals", request)
                valid_ticket(ticket, request)
                p["approval_id"] = ticket["approval_id"]
                self.store.save()
            ticket = self.api.call(
                "GET", "/operation-approvals/" + segment(p["approval_id"])
            )
            valid_ticket(ticket, request)
            self.store.evidence("approval", ticket)
            if ticket.get("revoked") or datetime.fromisoformat(
                ticket["expires_at"].replace("Z", "+00:00")
            ) <= datetime.now(timezone.utc):
                raise Stop(
                    "Approval expired/revoked; server has no renewal endpoint. "
                    "Retain state for operator recovery; no work submitted"
                )
            if (
                ticket["result"] == "pending"
                and self.approve_own_plans
                and ticket.get("requires_distinct_approver") is False
                and ticket.get("can_decide") is True
            ):
                ticket = self.api.call(
                    "POST",
                    "/operation-approvals/" + segment(p["approval_id"]) + "/decision",
                    {"result": "allowed-once"},
                )
                valid_ticket(ticket, request)
                self.store.evidence("requester-decision", ticket)
            if ticket["result"] == "pending":
                raise Stop(
                    f"Awaiting explicit approval: {ticket['approval_id']} "
                    f"(plan digest {ticket['plan_digest']})"
                )
            if ticket["result"] != "allowed-once":
                raise Stop("Approval rejected; no work submitted")
            body = {**p["preview_body"], "approval_id": p["approval_id"]}
            if p["kind"] != "continuation":
                body["plan_revision"] = review["revision"]
            p["submission"] = body
            self.store.save()
        # Replaying precisely this persisted admission also repairs a transaction
        # whose operation committed but workspace registration/response was lost.
        response = self.api.call("POST", p["path"], p["submission"])
        self.store.evidence("admission", response)
        expected_workspace = p["review"]["approval_request"]["workspace_id"]
        workspace = response.get("workspace_id", response.get("id"))
        if workspace != expected_workspace:
            raise Stop("Admission workspace differs from reviewed workspace")
        operation = (
            response.get("provisioning_operation_id")
            or response.get("control_operation_id")
            or response.get("operation_id")
        )
        if not operation:
            raise Stop(
                "Admission returned no durable operation ID; retain this request"
            )
        p["operation_id"] = operation
        self.s["workspace_id"] = workspace
        self.s["active"] = p
        self.s.setdefault("history", []).append(p)
        del self.s["pending"]
        self.store.save()

    def operation(self, stage):
        key = stage["review"]["approval_request"]["idempotency_key"]
        result = self.api.call("GET", "/operations/by-idempotency/" + segment(key))
        if (
            result.get("request_id") != key
            or result.get("workspace_id") != self.s["workspace_id"]
            or result.get("provisioning_operation_id") != stage["operation_id"]
        ):
            raise Stop("Recovered operation identity differs from saved admission")
        self.store.evidence("operation", result)
        return result

    def workspace(self):
        response = self.api.call(
            "GET", "/workspaces/" + segment(self.s["workspace_id"])
        )
        if (
            response.get("id") != self.s["workspace_id"]
            or response.get("org_id") != self.s["identity"]["org_id"]
        ):
            raise Stop("Workspace response scope changed")
        self.store.evidence("workspace", response)
        return response

    def verify_ready(self):
        root = "/workspaces/" + segment(self.s["workspace_id"])
        # A new invocation/token can repeat this, proving durable re-entry. Nodes
        # uses the authenticated broker and reads the actual Kubernetes API.
        for _ in range(2):
            workspace = self.workspace()
            if workspace["status"].lower() != "active":
                raise Stop("Workspace is no longer active")
        nodes = self.api.call("GET", root + "/nodes")
        if not isinstance(nodes.get("nodes"), list):
            raise Stop("Broker returned an invalid node inventory")
        self.store.evidence("broker-nodes", nodes)
        if not nodes["nodes"] or not all(
            node.get("ready") == "True" for node in nodes["nodes"]
        ):
            raise Stop(
                "Broker inventory has no nodes or includes a node that is not Ready"
            )
        self.store.evidence("lifecycle", self.api.call("GET", root + "/lifecycle"))
        self.s["ready_verified_at"] = now()
        self.store.save()
        print(
            "Workspace active; API re-entry, operation recovery and broker access verified."
        )

    def step(self, retire=False):
        if retire and (
            "workspace_id" not in self.s
            or (
                "pending" in self.s
                and not self.s["pending"]["kind"].startswith("retirement")
            )
        ):
            raise Stop("Finish workspace creation with run before starting retirement")
        if not retire and any(
            h["kind"].startswith("retirement") for h in self.s.get("history", [])
        ):
            raise Stop("Retirement has started; resume with retire")
        if "pending" in self.s:
            if self.s["pending"]["kind"].startswith("retirement") and not retire:
                raise Stop("Retirement is pending; resume with retire")
            self.stage()
        if "active" not in self.s:
            capabilities = self.api.call("GET", "/capabilities")
            self.store.evidence("capabilities", capabilities)
            if capabilities.get(
                "ready"
            ) is not True or "create-operation-id-v1" not in capabilities.get(
                "features", []
            ):
                raise Stop(
                    "Native runtime is not ready; installation/activation must finish first"
                )
            if retire:
                raise Stop("No workspace exists in this demo state")
            self.start_stage("create", "/workspaces", self.s["request"])
            self.stage()
        active = self.s["active"]
        operation = self.operation(active)
        if operation["state"] == "unknown":
            raise Stop(
                "Operation outcome is unknown; reconcile the saved operation without recreating it"
            )
        if operation["state"] in {"failed", "refused", "cancelled", "expired"}:
            raise Stop(
                f"Operation ended in {operation['state']}; inspect saved evidence"
            )
        if operation["state"] != "succeeded":
            print(f"{active['kind']}: {operation['state']}", flush=True)
            return False
        workspace = self.workspace()
        root = "/workspaces/" + segment(self.s["workspace_id"])
        if active["kind"] == "retirement":
            if workspace["status"] != "Deleted":
                print(
                    "Retirement operation succeeded; waiting for final Deleted projection.",
                    flush=True,
                )
                return False
            snapshot = self.api.call("GET", root + "/lifecycle")
            self.store.evidence("retirement-lifecycle", snapshot)
            self.s["retirement_api_verified_at"] = now()
            self.store.save()
            print(
                "Retirement operation succeeded and workspace is Deleted. "
                "API evidence does not independently prove stopped billing."
            )
            return True
        if workspace["status"].lower() == "active":
            if not retire:
                self.verify_ready()
                return True
            if "retirement_request_id" not in self.s:
                self.s["retirement_request_id"] = str(uuid.uuid4())
                self.store.save()
            access_done = active["kind"] == "retirement-access"
            kind = "retirement" if access_done else "retirement-access"
            path = root + ("/retirement" if access_done else "/retirement/access")
            self.start_stage(
                kind, path, {"operation_id": self.s["retirement_request_id"]}
            )
            self.stage()
            return False
        if retire:
            raise Stop(
                "Workspace must finish becoming active before retirement preparation"
            )
        proposals = self.api.call("GET", root + "/lifecycle-proposals")["proposals"]
        candidates = [
            p for p in proposals if p["source_operation_id"] == active["operation_id"]
        ]
        if not candidates:
            if workspace["status"].lower() in {
                "failed",
                "max_retries_exceeded",
                "deleted",
                "retired",
            }:
                raise Stop(f"Workspace status is {workspace['status']}")
            print(
                "Waiting for the next lifecycle proposal or active workspace.",
                flush=True,
            )
            return False
        if len(candidates) != 1:
            raise Stop(
                "Multiple lifecycle proposals; explicit operator review is required"
            )
        proposal = candidates[0]
        path = root + "/lifecycle-proposals/" + segment(proposal["artifact_id"])
        self.start_stage(
            "continuation",
            path + "/continue",
            {"operation_id": str(uuid.uuid4())},
            preview_path=path + "/preview",
        )
        self.stage()
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=".superplane-demo")
    parser.add_argument("--token-file", default=os.environ.get("SUPERPLANE_TOKEN_FILE"))
    parser.add_argument(
        "--base-url", help="Full API root, e.g. https://adp.example/api/superplane/v1"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="Save a real request identity, without provisioning"
    )
    init.add_argument("--request", required=True, help="Workspace request JSON file")
    for name in ("run", "retire"):
        command = commands.add_parser(name)
        command.add_argument("--timeout", type=float, default=60)
        command.add_argument("--interval", type=float, default=5)
        command.add_argument(
            "--approve-own-plans",
            action="store_true",
            help="Explicitly approve reviewed stage plans as the authenticated requester when server policy permits",
        )
    commands.add_parser("status")
    for name in ("review", "decide"):
        command = commands.add_parser(name)
        command.add_argument("--approval-id", required=True)
        if name == "decide":
            command.add_argument("--plan-digest", required=True)
            command.add_argument(
                "--decision", choices=("allowed-once", "rejected"), required=True
            )
    args = parser.parse_args(argv)
    store = None
    try:
        if not args.token_file:
            raise Stop(
                "Set SUPERPLANE_TOKEN_FILE or --token-file (never put tokens in arguments)"
            )
        if args.command in {"review", "decide"}:
            if not args.base_url:
                raise Stop("--base-url is required for a separate approver")
            api = API(args.base_url, args.token_file)
            ticket = api.call(
                "GET", "/operation-approvals/" + segment(args.approval_id)
            )
            if args.command == "review":
                print(json.dumps(ticket, indent=2))
            else:
                if (
                    ticket.get("plan_digest") != args.plan_digest
                    or ticket.get("can_decide") is not True
                ):
                    raise Stop(
                        "Digest changed or this human cannot decide the approval"
                    )
                result = api.call(
                    "POST",
                    "/operation-approvals/" + segment(args.approval_id) + "/decision",
                    {"result": args.decision},
                )
                print(f"Approval {result['approval_id']}: {result['result']}")
            return 0
        store = Store(args.state_dir)
        url = args.base_url or store.data.get("base_url")
        if not url:
            raise Stop("--base-url is required for initialization")
        api = API(url, args.token_file)
        if args.command == "init":
            if store.data:
                raise Stop(
                    "State already exists; use run/status with this state directory"
                )
            request = json.loads(Path(args.request).read_text())
            if not isinstance(request, dict) or not request.get("name"):
                raise Stop("Workspace request must be a JSON object with a name")
            if any(
                k in request for k in ("operation_id", "approval_id", "plan_revision")
            ):
                raise Stop(
                    "Omit operation_id, approval_id and plan_revision; this script manages them"
                )
            request["operation_id"] = str(uuid.uuid4())
            store.data.update(
                version=1,
                base_url=api.url,
                identity=api.identity(),
                request=request,
                created_at=now(),
                history=[],
            )
            store.save()
            print(f"Saved request {request['operation_id']}; no workspace created.")
            return 0
        demo = Demo(
            api, store, approve_own_plans=getattr(args, "approve_own_plans", False)
        )
        demo.bind()
        if args.command == "status":
            result = {
                "request_id": demo.s["request"]["operation_id"],
                "workspace_id": demo.s.get("workspace_id"),
                "pending": demo.s.get("pending"),
                "ready_verified_at": demo.s.get("ready_verified_at"),
                "retirement_api_verified_at": demo.s.get("retirement_api_verified_at"),
            }
            if "active" in demo.s:
                result["operation"] = demo.operation(demo.s["active"])
                result["workspace"] = demo.workspace()
            else:
                result["capabilities"] = api.call("GET", "/capabilities")
            print(json.dumps(result, indent=2))
            return 0
        if args.timeout < 0 or args.interval <= 0:
            raise Stop("Timeout must be nonnegative and interval positive")
        deadline = time.monotonic() + args.timeout
        while True:
            if demo.step(retire=args.command == "retire"):
                return 0
            if time.monotonic() >= deadline:
                raise Stop("Wait timed out; rerun with the same state directory")
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    except (Stop, OSError, ValueError, KeyError, TypeError) as error:
        # Do not print arbitrary input or exception reprs (token files, responses).
        message = (
            str(error)
            if isinstance(error, Stop)
            else f"Invalid local/API data ({type(error).__name__}); state retained"
        )
        print(message, file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
