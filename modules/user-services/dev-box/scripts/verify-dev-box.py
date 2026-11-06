#!/usr/bin/env python3
"""Live Dev Box acceptance for the self-managed stage (#7998, AC-01; #7223 re-run).

Creates (or resumes) the operator's own box through the gateway API exactly as the
UI does, waits for it to be ready, checks that the machine is reachable through
Session Manager and whether Kiro Crew on it reports ready, then releases it.
Standard library only; AWS lookups go through the aws CLI the operator already
has. The gateway token comes from --token, ADP_TOKEN or `adp token`.

  verify-dev-box.py [--env dev] [--region us-east-1] [--gateway-url https://.../api]
                    [--template assistant] [--profile crew]
                    [--ready-timeout 900] [--release-timeout 600] [--poll 10]
                    [--retain | --delete] [--keep] [--require-crew] [--skip-ssm-check]

Exit codes
  0  box reached running, SSM reports the instance Online, box released (Crew
     readiness reported; not required unless --require-crew)
  1  the box did not reach ready in time, an operation failed, or SSM never saw
     the instance Online (the box is still released when possible)
  2  prerequisite: no token / gateway URL, admission denied by policy, API error
  3  box ready and released but Crew did not report ready (only with --require-crew)
  4  release was accepted but not completed within --release-timeout; the box is
     still reserved — re-run with the same owner or release it from the UI
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

OK, FAILED, PREREQ, CREW_NOT_READY, RELEASE_PENDING = 0, 1, 2, 3, 4


class ApiError(RuntimeError):
    def __init__(self, status: int, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class Gateway:
    """Minimal authenticated JSON client; injectable for tests via Verifier(http=...)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def __call__(self, method: str, path: str, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - operator-selected gateway URL
                payload = response.read()
                return response.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                parsed = json.loads(payload) if payload else None
            except ValueError:
                parsed = payload.decode(errors="replace")
            raise ApiError(error.code, parsed) from None


def run_aws(args: list[str]) -> object:
    result = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"aws {' '.join(args)} failed")
    return json.loads(result.stdout or "null")


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("ADP_TOKEN"):
        return os.environ["ADP_TOKEN"]
    adp = shutil.which("adp") or os.path.expanduser("~/.adp/bin/adp")
    if not os.path.exists(adp):
        raise RuntimeError("no gateway token: pass --token, set ADP_TOKEN, or install the adp CLI and `adp login`")
    result = subprocess.run([adp, "token"], capture_output=True, text=True, check=False)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError("`adp token` did not return a token; run `adp login` first")
    return token


def resolve_gateway_url(explicit: str | None, environment: str, region: str, aws=run_aws) -> str:
    if explicit:
        return explicit
    if os.environ.get("ADP_GATEWAY_URL"):
        return os.environ["ADP_GATEWAY_URL"]
    parameter = aws(["ssm", "get-parameter", "--region", region, "--name", f"/adp/{environment}/gateway/cloudfront-domain"])
    domain = (parameter or {}).get("Parameter", {}).get("Value")
    if not domain:
        raise RuntimeError(f"no gateway URL: pass --gateway-url or publish /adp/{environment}/gateway/cloudfront-domain")
    return f"https://{domain}/api"


def resolve_deployment_id(explicit: str | None, environment: str, region: str, aws=run_aws) -> str:
    if explicit:
        return explicit
    parameter = aws(["ssm", "get-parameter", "--region", region, "--name", f"/adp/{environment}/dev-box/deployment-id"])
    value = (parameter or {}).get("Parameter", {}).get("Value", "").strip()
    if not value:
        raise RuntimeError(f"/adp/{environment}/dev-box/deployment-id is not published; run the Dev Box stage first")
    return value


class Verifier:
    def __init__(self, http, *, aws=run_aws, sleep=time.sleep, clock=time.monotonic, log=None):
        self.http = http
        self.aws = aws
        self.sleep = sleep
        self.clock = clock
        self.log = log or (lambda message: print(f"verify-dev-box: {message}", flush=True))
        self.summary: dict = {}

    def _poll(self, timeout: float, poll: float, check):
        deadline = self.clock() + timeout
        while True:
            outcome = check()
            if outcome is not None:
                return outcome
            if self.clock() >= deadline:
                return None
            self.sleep(poll)

    def wait_operation(self, desktop_id: str, operation_id: str, timeout: float, poll: float):
        def check():
            _, operation = self.http("GET", f"/cloud-desktops/{desktop_id}/operations/{operation_id}")
            self.log(f"operation {operation_id}: {operation['status']} ({operation.get('progress')})")
            if operation["status"] in ("succeeded", "failed"):
                return operation
            return None

        return self._poll(timeout, poll, check)

    def wait_running(self, desktop_id: str, timeout: float, poll: float):
        def check():
            _, box = self.http("GET", f"/cloud-desktops/{desktop_id}")
            self.log(f"box {desktop_id}: status={box['status']} observed={box.get('observed_state')}")
            if box["status"] in ("failed", "released"):
                return box
            if box["status"] == "running" and box.get("observed_state") == "running":
                return box
            return None

        return self._poll(timeout, poll, check)

    def ssm_online(self, deployment_id: str, region: str) -> bool:
        reservations = self.aws([
            "ec2", "describe-instances", "--region", region,
            "--filters", f"Name=tag:adp:deployment-id,Values={deployment_id}", "Name=tag:adp:component,Values=dev-box",
            "Name=instance-state-name,Values=running",
        ])
        instance_ids = [i["InstanceId"] for r in (reservations or {}).get("Reservations", []) for i in r.get("Instances", [])]
        self.summary["instances"] = instance_ids
        if not instance_ids:
            self.log("no running dev-box instance carries the deployment tags")
            return False
        info = self.aws(["ssm", "describe-instance-information", "--region", region,
                         "--filters", f"Key=InstanceIds,Values={','.join(instance_ids)}"])
        online = [i["InstanceId"] for i in (info or {}).get("InstanceInformationList", []) if i.get("PingStatus") == "Online"]
        self.summary["ssm_online"] = online
        self.log(f"SSM Online: {online or 'none'}")
        return bool(online)

    def crew_ready(self, desktop_id: str, machine_generation: int) -> bool:
        try:
            _, target = self.http("GET", "/crew/target")
        except ApiError as error:
            if error.status in (404, 409):
                self.log(f"Crew target not ready yet (HTTP {error.status})")
                return False
            raise
        lease = (target or {}).get("lease") or {}
        ready = (target or {}).get("health") == "ready" and lease.get("target_kind") == "ec2-instance" \
            and lease.get("target_ref") == desktop_id and lease.get("generation") == machine_generation
        self.summary["crew"] = {"health": (target or {}).get("health"), "target_kind": lease.get("target_kind")}
        self.log(f"Crew: health={(target or {}).get('health')} kind={lease.get('target_kind')} ready={ready}")
        return ready

    def release(self, desktop_id: str, retention: str, timeout: float, poll: float) -> int:
        try:
            _, accepted = self.http("POST", f"/cloud-desktops/{desktop_id}/release",
                                    {"workspace_retention": retention}, {"Idempotency-Key": str(uuid.uuid4())})
        except ApiError as error:
            self.log(f"release refused: {error}")
            return RELEASE_PENDING
        operation = self.wait_operation(desktop_id, accepted["operation_id"], timeout, poll)
        if operation is None:
            self.log(f"release accepted ({accepted['operation_id']}) but not completed in {timeout}s; the box stays reserved")
            return RELEASE_PENDING
        if operation["status"] != "succeeded":
            self.log(f"release failed: {operation.get('error_class')}")
            return RELEASE_PENDING
        self.summary["released"] = True
        return OK

    def run(self, *, template: str, profile: str, deployment_id: str, region: str, ready_timeout: float,
            release_timeout: float, poll: float, retention: str, keep: bool, require_crew: bool, skip_ssm: bool) -> int:
        try:
            _, policy = self.http("GET", "/cloud-desktops/policy")
        except ApiError as error:
            self.log(f"policy check failed: {error}")
            return PREREQ
        self.summary["policy"] = policy
        _, listing = self.http("GET", "/cloud-desktops")
        current = [box for box in (listing or {}).get("items", []) if box.get("status") != "released"]
        if not current and not policy.get("admission_allowed"):
            self.log(f"admission denied: {policy.get('reason')}")
            return PREREQ
        try:
            status, admitted = self.http("POST", "/cloud-desktops/ensure", {"template": template, "profile": profile},
                                         {"Idempotency-Key": str(uuid.uuid4())})
        except ApiError as error:
            self.log(f"ensure refused: {error}")
            return PREREQ
        desktop_id = admitted["desktop_id"]
        self.summary["desktop_id"] = desktop_id
        self.log(f"ensure -> HTTP {status} desktop {desktop_id} operation {admitted.get('operation_id')}")
        result = OK
        if admitted.get("operation_id"):
            operation = self.wait_operation(desktop_id, admitted["operation_id"], ready_timeout, poll)
            if operation is None or operation["status"] != "succeeded":
                self.log("setup/start operation did not succeed" if operation else f"operation still pending after {ready_timeout}s")
                result = FAILED
        box = None
        if result == OK:
            box = self.wait_running(desktop_id, ready_timeout, poll)
            if box is None or box["status"] != "running":
                self.log("box did not reach running" if box is None else f"box ended {box['status']}")
                result = FAILED
        if result == OK and not skip_ssm and not self.ssm_online(deployment_id, region):
            result = FAILED
        if result == OK:
            crew = self.crew_ready(desktop_id, box["machine_generation"])
            self.summary["crew_ready"] = crew
            if require_crew and not crew:
                result = CREW_NOT_READY
        if keep:
            self.log("--keep: leaving the box reserved")
        else:
            released = self.release(desktop_id, retention, release_timeout, poll)
            if released != OK:
                result = released if result == OK else result
                self.summary["released"] = False
        self.summary["result"] = result
        print(json.dumps(self.summary, sort_keys=True, default=str))
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=os.environ.get("ADP_ENV", "dev"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--gateway-url")
    parser.add_argument("--token")
    parser.add_argument("--deployment-id")
    parser.add_argument("--template", default="assistant")
    parser.add_argument("--profile", default="crew")
    parser.add_argument("--ready-timeout", type=float, default=900)
    parser.add_argument("--release-timeout", type=float, default=600)
    parser.add_argument("--poll", type=float, default=10)
    retention = parser.add_mutually_exclusive_group()
    retention.add_argument("--retain", action="store_const", dest="retention", const="retain")
    retention.add_argument("--delete", action="store_const", dest="retention", const="delete")
    parser.set_defaults(retention="delete")
    parser.add_argument("--keep", action="store_true", help="do not release the box afterwards")
    parser.add_argument("--require-crew", action="store_true", help="exit 3 unless Crew reports ready on the box")
    parser.add_argument("--skip-ssm-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        token = resolve_token(args.token)
        url = resolve_gateway_url(args.gateway_url, args.env, args.region)
        deployment_id = resolve_deployment_id(args.deployment_id, args.env, args.region)
    except RuntimeError as error:
        print(f"verify-dev-box: {error}", file=sys.stderr)
        return PREREQ
    verifier = Verifier(Gateway(url, token))
    try:
        return verifier.run(template=args.template, profile=args.profile, deployment_id=deployment_id, region=args.region,
                            ready_timeout=args.ready_timeout, release_timeout=args.release_timeout, poll=args.poll,
                            retention=args.retention, keep=args.keep, require_crew=args.require_crew, skip_ssm=args.skip_ssm_check)
    except (ApiError, RuntimeError) as error:
        print(f"verify-dev-box: {error}", file=sys.stderr)
        return PREREQ


if __name__ == "__main__":
    sys.exit(main())
