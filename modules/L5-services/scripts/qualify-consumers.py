#!/usr/bin/env python3
"""Bounded installed consumer checks; no provider jobs or deployment changes."""

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import boto3
import httpx
from botocore.config import Config
from l5_services.publication.installed import qualification_cases
from l5_services.workers.consumer_fixture import PROTOCOL, digest

REPO = Path(__file__).resolve().parents[3]
DENIED = {403, 404}


class CheckFailed(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def private_path(value, *, existing=True):
    path = Path(value).expanduser()
    require(
        path.is_absolute() and not path.is_symlink(),
        "Use an absolute private file path without a symlink",
    )
    resolved = path.resolve()
    require(
        not resolved.is_relative_to(REPO),
        "Keep credentials, fixture IDs and evidence outside the checkout",
    )
    if existing:
        info = path.stat()
        require(
            path.is_file() and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
            "Private input files must belong to the operator and have mode 0600",
        )
    else:
        require(
            path.parent.is_dir() and not path.exists(),
            "Choose a new output file in an existing private directory",
        )
    return path


def read_json(value):
    path = private_path(value)
    require(
        path.stat().st_size <= 4 * 1024 * 1024,
        "Private fixture or evidence exceeds the size limit",
    )
    return json.loads(path.read_text())


def read_token(value):
    path = private_path(value)
    require(path.stat().st_size <= 65536, "Token file exceeds the size limit")
    raw = path.read_text().strip()
    token = json.loads(raw).get("access_token") if raw.startswith("{") else raw
    require(
        isinstance(token, str)
        and 20 <= len(token) <= 32768
        and not re.search(r"\s", token),
        "Token file must contain one current access token or an access_token JSON property",
    )
    return token


def validate_fixture(fixture):
    required = {
        "protocol",
        "expected_account",
        "region",
        "repair_function",
        "expected_code_sha256",
        "expected_role_arn",
        "service_id",
        "cases",
        "gateway_url",
        "owner_token_file",
        "foreign_token_file",
        "reads",
    }
    require(
        isinstance(fixture, dict)
        and set(fixture) == required
        and fixture["protocol"] == PROTOCOL,
        "Fixture must contain exactly the documented qualification fields",
    )
    require(
        re.fullmatch(r"[0-9]{12}", fixture["expected_account"]) is not None,
        "Pin the target AWS account",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9+/]{43}=", fixture["expected_code_sha256"])
        is not None,
        "Pin the installed repair ZIP CodeSha256",
    )
    prefix = (
        f"arn:aws:lambda:{fixture['region']}:{fixture['expected_account']}:function:"
    )
    require(
        fixture["repair_function"].startswith(prefix)
        and ":" not in fixture["repair_function"][len(prefix) :],
        "Select the exact unqualified repair function ARN in the target account and region",
    )
    require(
        fixture["expected_role_arn"].startswith(
            f"arn:aws:iam::{fixture['expected_account']}:role/"
        ),
        "Pin the installed repair IAM role in the target account",
    )
    parsed = urlsplit(fixture["gateway_url"])
    require(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"},
        "Gateway must be an HTTPS origin without credentials or a path",
    )
    cases = fixture["cases"]
    qualification_cases(cases, fixture["service_id"], "product-context")
    require(
        all(case["service_kind"] == fixture["service_id"] for case in cases),
        "All cases must belong to this installed service",
    )
    require(
        len({case["contribution_id"] for case in cases}) == len(cases),
        "Duplicate contribution IDs are not supported",
    )
    reads = fixture["reads"]
    fields = {
        "contribution_id",
        "task_id",
        "foreign_task_id",
        "foreign_product_id",
        "foreign_project_id",
    }
    require(
        isinstance(reads, list) and len(reads) == len(cases),
        "Pin one readback selection for every contribution",
    )
    require(
        all(
            isinstance(item, dict)
            and set(item) == fields
            and all(isinstance(value, str) and value for value in item.values())
            for item in reads
        ),
        "Readback selections must pin later task and foreign Product/task IDs",
    )
    require(
        {item["contribution_id"] for item in reads}
        == {case["contribution_id"] for case in cases},
        "Readback contributions differ from worker selections",
    )
    for case in cases:
        item = next(
            item for item in reads if item["contribution_id"] == case["contribution_id"]
        )
        require(
            item["foreign_product_id"] != case["scope"]["product_id"]
            and item["task_id"] != item["foreign_task_id"]
            and item["task_id"] != case["run_id"],
            "Foreign and causal-independent selections must be distinct",
        )
    return {
        key: fixture[key]
        for key in (
            "expected_account",
            "region",
            "repair_function",
            "expected_code_sha256",
            "expected_role_arn",
            "service_id",
            "cases",
            "gateway_url",
            "reads",
        )
    }


class Checker:
    def __init__(self, fixture, aws, http, owner_token, foreign_token):
        self.fixture, self.aws, self.http = fixture, aws, http
        self.tokens = {"owner": owner_token, "foreign": foreign_token}
        self.step = "configuration"
        self.selection = validate_fixture(fixture)
        self.case_digest = digest(fixture["cases"])
        self.client = aws.client(
            "lambda",
            region_name=fixture["region"],
            config=Config(
                read_timeout=900, connect_timeout=10, retries={"max_attempts": 0}
            ),
        )

    def configuration(self):
        observed = self.client.get_function_configuration(
            FunctionName=self.fixture["repair_function"]
        )
        env = observed.get("Environment", {}).get("Variables", {})
        require(
            observed.get("FunctionArn") == self.fixture["repair_function"]
            and observed.get("CodeSha256") == self.fixture["expected_code_sha256"]
            and observed.get("Role") == self.fixture["expected_role_arn"]
            and observed.get("Handler") == "l5_services.workers.recovery.lambda_handler"
            and observed.get("State") == "Active"
            and observed.get("LastUpdateStatus") == "Successful"
            and env.get("L5_SERVICE_ID") == self.fixture["service_id"],
            "Installed repair code, identity or state differs from the pinned fixture; finish installation first",
        )
        require(
            json.loads(env.get("L5_CONSUMER_QUALIFICATION_CASES", "null"))
            == self.fixture["cases"],
            "Install exactly the selected existing outbox cases before qualification",
        )
        require(
            not json.loads(env.get("L5_CONSUMER_CONTRACTS", "{}"))
            and not json.loads(env.get("L5_WA_PEER_CONTRACTS", "{}")),
            "Broad consumer contracts must remain disabled during finite qualification",
        )
        return {
            key: observed[key]
            for key in ("FunctionArn", "CodeSha256", "Role", "RevisionId", "Version")
        }

    def invoke(self, event):
        response = self.client.invoke(
            FunctionName=self.fixture["repair_function"],
            InvocationType="RequestResponse",
            Qualifier=self.identity["Version"],
            Payload=json.dumps(event).encode(),
        )
        require(
            response.get("StatusCode") == 200 and not response.get("FunctionError"),
            "Installed repair rejected the bounded operation; inspect its private logs and owed outbox state",
        )
        require(
            response.get("ExecutedVersion") == self.identity["Version"],
            "Repair function version changed during qualification",
        )
        raw = response["Payload"].read(4 * 1024 * 1024 + 1)
        require(
            len(raw) <= 4 * 1024 * 1024, "Repair observation exceeds the size limit"
        )
        return json.loads(raw)

    def phase(self, phase):
        self.step = "worker_" + phase
        report = self.invoke(
            {"l5_consumer_fixture": 1, "phase": phase, "cases_digest": self.case_digest}
        )
        require(
            report.get("protocol") == PROTOCOL
            and report.get("phase") == phase
            and report.get("cases_digest") == self.case_digest
            and report.get("service_id") == self.fixture["service_id"],
            "Installed repair returned observations for a different selection",
        )
        observations = report.get("observations", [])
        require(
            len(observations) == len(self.fixture["cases"]),
            "Installed repair omitted a selected contribution",
        )
        by_id = {item["contribution_id"]: item for item in observations}
        require(
            set(by_id) == {case["contribution_id"] for case in self.fixture["cases"]},
            "Installed repair reported another contribution",
        )
        for case in self.fixture["cases"]:
            item = by_id[case["contribution_id"]]
            require(
                item.get("destination") == case["destination"]
                and item.get("contribution_hash") == case["contribution_hash"],
                "Installed repair returned different immutable lineage",
            )
        return by_id

    def request(self, method, path, *, actor="owner", denied=False):
        response = self.http.request(
            method,
            path,
            headers={"Authorization": "Bearer " + self.tokens[actor]},
            content=b"" if method == "POST" else None,
        )
        if denied:
            require(
                response.status_code in DENIED,
                "Foreign or revoked consumer access was not denied; keep consumers disabled",
            )
            return response.status_code
        require(
            response.status_code == 200,
            "Owner read/use failed; verify Product authority, later Task enrollment and standing policy",
        )
        require(
            len(response.content) <= 32 * 1024 * 1024,
            "Consumer readback exceeds the size limit",
        )
        return response.json()

    def readbacks(self, observations, *, revoked=False):
        output, retrievals = [], {}
        for case in sorted(
            self.fixture["cases"],
            key=lambda item: item["destination"] != "product-context",
        ):
            self.step = "revoked_readback" if revoked else "owner_and_foreign_readback"
            item = next(
                item
                for item in self.fixture["reads"]
                if item["contribution_id"] == case["contribution_id"]
            )
            scope = case["scope"]

            def path(product, project):
                values = [
                    quote(value, safe="")
                    for value in (
                        product,
                        project,
                        case["service_kind"],
                        case["contribution_id"],
                    )
                ]
                return f"/v1/products/{values[0]}/projects/{values[1]}/services/{values[2]}/contributions/{values[3]}/consumer"

            base = path(scope["product_id"], scope["project_id"])
            foreign = path(item["foreign_product_id"], item["foreign_project_id"])
            task_path = "/tasks/" + quote(item["task_id"], safe="")
            denied = {}
            for suffix in ("/receipt", "/projection", task_path):
                denied["foreign_actor" + suffix] = self.request(
                    "GET", base + suffix, actor="foreign", denied=True
                )
                denied["foreign_product" + suffix] = self.request(
                    "GET", foreign + suffix, denied=True
                )
            denied["foreign_task"] = self.request(
                "GET",
                base + "/tasks/" + quote(item["foreign_task_id"], safe=""),
                denied=True,
            )
            observed = {"contribution_id": case["contribution_id"], "denials": denied}
            if revoked:
                for suffix in ("/receipt", "/projection", task_path):
                    denied["revoked_owner" + suffix] = self.request(
                        "GET", base + suffix, denied=True
                    )
                denied["revoked_use"] = self.request(
                    "POST", base + task_path, denied=True
                )
            else:
                receipt = self.request("GET", base + "/receipt")
                require(
                    receipt == observations[case["contribution_id"]]["receipt"],
                    "Owner receipt differs from original durable admission",
                )
                projection = self.request("GET", base + "/projection")
                require(
                    projection.pop("contribution_hash", None)
                    == case["contribution_hash"]
                    and digest(projection) == case["contribution_hash"],
                    "Consumer projection differs from the complete immutable producer envelope",
                )
                self.step = "later_task_use"
                usage = self.request("POST", base + task_path)
                require(
                    usage == self.request("GET", base + task_path),
                    "Later-task receipt is not durably readable",
                )
                require(
                    all(
                        usage.get(key) == value
                        for key, value in {
                            "scope": scope,
                            "result_ref": case["result_ref"],
                            "contribution_id": case["contribution_id"],
                            "task_id": item["task_id"],
                            "actor_id": scope["owner_id"],
                        }.items()
                    )
                    and usage.get("receipt_id")
                    and usage.get("citation"),
                    "Later-task receipt has different authority or lineage",
                )
                lineage = digest(
                    {
                        "scope": scope,
                        "result_ref": case["result_ref"],
                        "task_id": item["task_id"],
                    }
                )
                if case["destination"] == "product-context":
                    retrievals[lineage] = usage["receipt_id"]
                else:
                    require(
                        lineage in retrievals
                        and usage.get("retrieval_receipt_id") == retrievals[lineage],
                        "Insights usage must reference this qualification's actual Context retrieval receipt",
                    )
                observed.update(
                    receipt=receipt, projection_digest=digest(projection), usage=usage
                )
            output.append(observed)
        return output

    def run(self, phase, baseline=None):
        self.step = "account_identity"
        require(
            self.aws.client("sts").get_caller_identity()["Account"]
            == self.fixture["expected_account"],
            "Active AWS profile targets a different account",
        )
        self.identity = self.configuration()
        self.step = "safe_runtime_probe"
        probe = self.invoke({"l5_runtime_probe": 1})
        require(
            probe
            == {
                "service_id": self.fixture["service_id"],
                "consumer_fixture_protocol": PROTOCOL,
            },
            "Installed worker lacks the safe bounded protocol; deploy it before sending fixture events",
        )
        selection_digest = digest(self.selection)
        if phase == "invalidate":
            require(
                isinstance(baseline, dict)
                and baseline.get("phase") == "admit"
                and baseline.get("phase_passed") is True
                and baseline.get("protocol") == PROTOCOL
                and baseline.get("selection_digest") == selection_digest,
                "Invalidation requires a passing admission/readback report for this exact installed selection",
            )
        first = self.phase(phase)
        second = self.phase("replay" if phase == "admit" else "late-replay")
        if phase == "admit":
            require(
                first == second,
                "Replay changed original receipts or native fingerprints",
            )
        else:
            for key, item in second.items():
                previous = baseline["worker_observations"][key]
                require(
                    item["receipt"] == previous["receipt"]
                    and item["native_fingerprint"] == previous["native_fingerprint"]
                    and item["applied_watermark"] > 0
                    and item["invalidation_receipt"]
                    and first[key]["denied_status"] in {403, 404, 409, 422}
                    and item["denied_status"] in {403, 404, 409, 410},
                    "Invalidation did not preserve original history, apply its fence, or reject late replay",
                )
        readbacks = self.readbacks(second, revoked=phase == "invalidate")
        self.step = "installation_readback"
        require(
            self.configuration() == self.identity,
            "Installed worker changed during qualification; repeat against one immutable release",
        )
        return {
            "protocol": PROTOCOL,
            "phase": phase,
            "phase_passed": True,
            "selection_digest": selection_digest,
            "installed_identity": self.identity,
            "cases_digest": self.case_digest,
            "worker_observations": second,
            "readbacks": readbacks,
            "bounded_consumer_checks_complete": phase == "invalidate",
            "native_acceptance_assessed": False,
            "completed_at": datetime.now(UTC).isoformat(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--phase", choices=("admit", "invalidate"), required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = private_path(args.output, existing=False)
    checker = None
    try:
        fixture = read_json(args.fixture)
        validate_fixture(fixture)
        baseline = read_json(args.baseline) if args.baseline else None
        require(
            (args.phase == "invalidate") == bool(args.baseline),
            "Use --baseline only for the invalidation phase",
        )
        with httpx.Client(
            base_url=fixture["gateway_url"],
            trust_env=False,
            follow_redirects=False,
            timeout=60,
        ) as http:
            checker = Checker(
                fixture,
                boto3.Session(profile_name=args.profile, region_name=fixture["region"]),
                http,
                read_token(fixture["owner_token_file"]),
                read_token(fixture["foreign_token_file"]),
            )
            result = checker.run(args.phase, baseline)
    except Exception as error:
        result = {
            "protocol": PROTOCOL,
            "phase": args.phase,
            "phase_passed": False,
            "bounded_consumer_checks_complete": False,
            "failed_step": checker.step if checker else "private_fixture",
            "operator_action": str(error)
            if isinstance(error, CheckFailed)
            else "Check the pinned private fixture and installed logs; resolve authority or installation errors before retrying",
            "error_type": type(error).__name__,
        }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(
        "Bounded consumer phase passed"
        if result["phase_passed"]
        else "Bounded consumer phase failed; see private output for the actionable step"
    )
    return 0 if result["phase_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
