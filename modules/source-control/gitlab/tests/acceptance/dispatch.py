#!/usr/bin/env python3
"""Prepare one owned qualification project or dispatch its reviewed executor suite.

Use only after the existing live-readiness gate. The exclusive private journal
survives lost provider responses; an ambiguous pipeline is never resubmitted.
"""
import argparse
import asyncio
import base64
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent
MODULE = ROOT.parents[1]
REPO = MODULE.parents[2]
CONFIG_PATH = "modules/source-control/gitlab/tests/acceptance/executor.gitlab-ci.yml"


class DispatchError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise DispatchError(code)


def private_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600, "private_operator_file_required")
    return json.loads(path.read_text())


def target_payload(config):
    target_file = Path(config["targets_file"])
    require(target_file.stat().st_size <= 32768, "qualification_targets_too_large")
    payload = private_json(target_file)
    require(isinstance(payload, dict) and set(payload) == {"run_id", "targets"}
            and payload["run_id"] == config["run_id"], "qualification_target_run_wrong")
    verifier = module_file("qualification_target_schema", ROOT / "verify.py")
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "fixtures.schema.json").read_text())["properties"]["network_targets"]
    targets = payload["targets"]
    require(Draft202012Validator(schema).is_valid(targets), "qualification_targets_invalid")
    expected = verifier.NEGATIVE_TARGETS | verifier.POSITIVE_TARGETS
    require({row["name"] for row in targets} == expected and len(targets) == len(expected)
            and all(row["expected"] in verifier.expected_outcomes(row["name"])
                    for row in targets), "qualification_target_set_wrong")
    for row in targets:
        try:
            address = ipaddress.ip_address(row["address"])
        except ValueError as error:
            raise DispatchError("qualification_target_address_invalid") from error
        name = row["name"]
        require((name not in verifier.METADATA_TARGETS or
                 (str(address), row["port"], row["kind"]) == verifier.METADATA_TARGETS[name])
                and (name not in {"gitlab", "doh"} or (row["kind"] == "https" and row.get("hostname")
                     and row.get("expected_status") == 200))
                and (name != "alternate_dns" or row["kind"] == "dns"), "qualification_target_protocol_wrong")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def module_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def validate_config(config):
    require(set(config) == {"run_id", "source_revision", "account_id", "expected_role_arn", "region",
            "gitlab_url", "ca_file", "tenant_id", "group_id", "runner_id", "runner_tag", "ami_id", "targets_file"},
            "qualification_config_fields_wrong")
    require(str(uuid.UUID(config["run_id"])) == config["run_id"], "run_id_invalid")
    require(re.fullmatch(r"[0-9a-f]{40}", config["source_revision"]), "source_revision_invalid")
    require(re.fullmatch(r"[0-9]{12}", config["account_id"]), "account_invalid")
    require(re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", config["region"]), "region_invalid")
    require(re.fullmatch(r"arn:aws:iam::" + config["account_id"] + r":role/[A-Za-z0-9+=,.@_/-]+",
                         config["expected_role_arn"]), "operator_role_invalid")
    require(type(config["group_id"]) is int and config["group_id"] > 0
            and type(config["runner_id"]) is int and config["runner_id"] > 0, "group_or_runner_invalid")
    require(config["runner_tag"] == f"adp-group-{config['group_id']}", "group_tag_invalid")
    require(isinstance(config["tenant_id"], str) and config["tenant_id"], "tenant_invalid")
    url = urlsplit(config["gitlab_url"])
    require(url.scheme == "https" and url.hostname and url.port in (None, 443)
            and not url.username and not url.password and not url.query and not url.fragment
            and url.path in ("", "/gitlab"), "gitlab_url_invalid")
    target_payload(config)


class Journal:
    def __init__(self, path, scope):
        self.path = path
        info = path.parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o700, "private_journal_directory_required")
        descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(descriptor, "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.scope = scope
            self.data = private_json(path) if path.exists() else {"scope": scope, "phase": "new"}
            require(self.data.get("scope") == scope, "journal_scope_conflict")
        except Exception:
            self.lock.close()
            raise

    def save(self, **changes):
        self.data.update(changes)
        temp = self.path.with_name(self.path.name + ".tmp-" + str(uuid.uuid4()))
        try:
            with temp.open("x") as output:
                os.chmod(temp, 0o600)
                json.dump(self.data, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temp.unlink(missing_ok=True)

    def close(self):
        self.lock.close()


class Qualification:
    def __init__(self, config, lock, admin, journal):
        self.config, self.lock, self.admin, self.journal = config, lock, admin, journal
        self.slug = "adp-runner-qualification-" + config["run_id"]
        self.ref = "qualification-" + config["run_id"]
        self.marker = f"ADP qualification {config['run_id']} source {config['source_revision']}"
        self.variables = {
            "ADP_QUALIFICATION_RUN_ID": config["run_id"], "ADP_QUALIFICATION_SUITE": "executor",
            "ADP_QUALIFICATION_JOB_IMAGE": lock["job_image"],
            "ADP_QUALIFICATION_HELPER_IMAGE": lock["helper_image"],
            "ADP_QUALIFICATION_RUNNER_TAG": config["runner_tag"],
            "ADP_QUALIFICATION_TARGETS_FILE": target_payload(config),
        }

    async def verify_group(self):
        registrar = module_file("qualification_registrar", MODULE / "scripts/register-runner.py")
        await registrar.verify_tenant_group(self.admin, self.config["tenant_id"], self.config["group_id"])
        return await self.admin.request("GET", f"groups/{self.config['group_id']}")

    async def verify_project(self, project):
        require(isinstance(project, dict) and type(project.get("id")) is int
                and project["id"] > 0 and project.get("path") == self.slug
                and project.get("description") == self.marker
                and project.get("namespace", {}).get("id") == self.config["group_id"]
                and project["namespace"].get("kind") == "group"
                and project.get("visibility") == "private" and not project.get("shared_with_groups")
                and project.get("shared_runners_enabled") is False
                and project.get("ci_config_path") == CONFIG_PATH
                and project.get("archived") is False, "owned_project_not_verified")
        for endpoint in ("members", "members/all"):
            members = await self.admin.pages(f"projects/{project['id']}/{endpoint}")
            require(isinstance(members, list) and all(row.get("id") == self.admin.owner_id
                    and row.get("access_level") in (40, 50) for row in members), "project_members_not_owned")
        return project

    async def prepare(self):
        group = await self.verify_group()
        path = "projects/" + quote(group["full_path"] + "/" + self.slug, safe="")
        project = await self.admin.request("GET", path, missing=True)
        if project is None:
            require(self.journal.data["phase"] == "new", "project_creation_ambiguous")
            self.journal.save(phase="project_intent")
            project = await self.admin.request("POST", "projects", json={
                "namespace_id": self.config["group_id"], "path": self.slug, "name": self.slug,
                "description": self.marker, "visibility": "private", "initialize_with_readme": False,
                "shared_runners_enabled": False, "ci_config_path": CONFIG_PATH,
            })
            project = await self.admin.request("GET", f"projects/{project['id']}")
        else:
            require(self.journal.data["phase"] != "new", "project_not_created_by_this_journal")
        await self.verify_project(project)
        if self.journal.data["phase"] in {"new", "project_intent"}:
            self.journal.save(phase="project_ready", project_id=project["id"])
        require(self.journal.data.get("project_id") == project["id"], "project_identity_changed")
        return {"phase": self.journal.data["phase"], "project_id": project["id"],
                "ref": self.ref, "source_revision": self.config["source_revision"]}

    async def validate_dispatch(self):
        await self.prepare()
        project = self.journal.data["project_id"]
        branch = await self.admin.request("GET", f"projects/{project}/repository/branches/{self.ref}")
        require(branch.get("commit", {}).get("id") == self.config["source_revision"], "pushed_source_wrong")
        for relative in (CONFIG_PATH, "modules/source-control/gitlab/tests/acceptance/executor_probe.py",
                         "modules/source-control/gitlab/tests/acceptance/probe.py"):
            data = await self.admin.request("GET", f"projects/{project}/repository/files/" + quote(relative, safe=""),
                                            params={"ref": self.config["source_revision"]})
            require(data.get("encoding") == "base64" and data.get("commit_id") == self.config["source_revision"]
                    and base64.b64decode(data["content"], validate=True) == (REPO / relative).read_bytes(),
                    "qualification_source_bytes_wrong")
        runner = await self.admin.request("GET", f"runners/{self.config['runner_id']}")
        require(runner.get("runner_type") == "group_type" and runner.get("locked") is True
                and runner.get("run_untagged") is False and runner.get("paused") is False
                and runner.get("status") == "online" and self.config["runner_tag"] in runner.get("tag_list", [])
                and [row["id"] for row in runner.get("groups", [])] == [self.config["group_id"]],
                "qualified_runner_not_ready")
        require(self.variables["ADP_QUALIFICATION_TARGETS_FILE"] == target_payload(self.config),
                "qualification_targets_changed")
        require(await self.admin.pages(f"projects/{project}/variables") == [], "project_variable_override_present")
        require(await self.admin.pages(f"groups/{self.config['group_id']}/variables") == [],
                "group_variable_override_present")

    async def matching_pipelines(self):
        project = self.journal.data["project_id"]
        rows = await self.admin.pages(f"projects/{project}/pipelines", ref=self.ref,
                                      sha=self.config["source_revision"])
        matches = []
        for row in rows:
            pipeline = await self.admin.request("GET", f"projects/{project}/pipelines/{row['id']}")
            require(pipeline.get("sha") == self.config["source_revision"] and pipeline.get("ref") == self.ref
                    and pipeline.get("source") == "api", "unexpected_owned_project_pipeline")
            variables = await self.admin.request("GET", f"projects/{project}/pipelines/{row['id']}/variables")
            actual = {item["key"]: item["value"] for item in variables}
            require(len(variables) == len(actual) and actual == self.variables
                    and all(item.get("variable_type") == ("file" if item["key"] == "ADP_QUALIFICATION_TARGETS_FILE"
                            else "env_var") for item in variables), "pipeline_bindings_wrong")
            matches.append(pipeline["id"])
        require(len(matches) <= 1, "duplicate_qualification_pipeline")
        return matches

    async def dispatch(self):
        await self.validate_dispatch()
        matches = await self.matching_pipelines()
        phase = self.journal.data["phase"]
        if phase == "project_ready":
            require(not matches, "pipeline_not_created_by_this_journal")
            self.journal.save(phase="pipeline_intent")
            pipeline = await self.admin.request("POST", f"projects/{self.journal.data['project_id']}/pipeline", json={
                "ref": self.ref, "variables": [{"key": key, "value": value,
                                               "variable_type": "file" if key == "ADP_QUALIFICATION_TARGETS_FILE" else "env_var"}
                                               for key, value in self.variables.items()],
            })
            matches = await self.matching_pipelines()
            require(matches == [pipeline.get("id")], "pipeline_response_not_verified")
        else:
            require(phase in {"pipeline_intent", "pipeline_created"}, "pipeline_phase_invalid")
        require(len(matches) == 1, "pipeline_creation_ambiguous_do_not_redispatch")
        require(self.journal.data.get("pipeline_id", matches[0]) == matches[0], "pipeline_identity_changed")
        self.journal.save(phase="pipeline_created", pipeline_id=matches[0])
        return {"phase": "pipeline_created", "project_id": self.journal.data["project_id"],
                "pipeline_id": matches[0], "run_id": self.config["run_id"], "acceptance": "not_evaluated"}


async def execute(args):
    import boto3
    config = private_json(args.config)
    validate_config(config)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, check=True).stdout.decode().strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=REPO,
                           capture_output=True, check=True).stdout
    require(head == config["source_revision"] and not dirty, "reviewed_clean_source_required")
    session = boto3.Session(profile_name=args.aws_profile, region_name=config["region"])
    identity = session.client("sts").get_caller_identity()
    role_name = config["expected_role_arn"].rsplit("/", 1)[-1]
    require(identity["Account"] == config["account_id"] and re.fullmatch(
        rf"arn:aws:sts::{config['account_id']}:assumed-role/{re.escape(role_name)}/[^/]+", identity["Arn"]),
        "selected_operator_identity_wrong")
    discovered = session.client("ssm").get_parameter(Name="/adp/dev/gitlab/runner-url")["Parameter"]["Value"]
    require(discovered == config["gitlab_url"], "gitlab_discovery_mismatch")
    release = module_file("qualification_release", MODULE / "runners/scripts/release_lock.py")
    lock = json.loads((MODULE / "runners/versions.lock.json").read_text())
    host = release.check(lock, config["region"], config["ami_id"], lock["job_image"])
    require(host["owner_id"] == config["account_id"], "ami_owner_mismatch")
    # verify_ami uses the CLI; explicitly preserve the selected SDK identity.
    def selected_run(command, **kwargs):
        return subprocess.run(command, env={**os.environ, "AWS_PROFILE": args.aws_profile,
                                            "AWS_REGION": config["region"]}, **kwargs)
    release.verify_ami(host, config["region"], run=selected_run)
    scope = hashlib.sha256(json.dumps({"config": config, "lock": lock}, sort_keys=True).encode()).hexdigest()
    journal = Journal(args.journal, scope)
    try:
        token = session.client("secretsmanager").get_secret_value(SecretId="adp/dev/gitlab-admin-token")["SecretString"]
        sys.path.insert(0, str(REPO / "modules/gateway"))
        from src.gitlab.admin_client import GitlabAdmin
        async with GitlabAdmin(config["gitlab_url"], token, ca_file=config["ca_file"]) as admin:
            operation = Qualification(config, lock, admin, journal)
            return await (operation.dispatch() if args.dispatch else operation.prepare())
    finally:
        journal.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--aws-profile", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--dispatch", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
    except Exception:
        # Never include provider response bodies, credentials or job traces.
        print(json.dumps({"phase": "incomplete", "reason": "qualification_dispatch_refused_reconcile_journal"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
