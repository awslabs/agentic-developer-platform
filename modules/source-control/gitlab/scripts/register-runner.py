#!/usr/bin/env python3
"""Operator-only GitLab group runner registration primitives."""

import hashlib
import re


class RegistrationError(Exception):
    """Safe error code without provider data or credential values."""


def group_tag(group_id):
    if type(group_id) is not int or group_id <= 0:
        raise RegistrationError("invalid_group_id")
    return f"adp-group-{group_id}"


async def verify_tenant_group(admin, tenant_id, group_id):
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 256:
        raise RegistrationError("invalid_tenant_id")
    tag = group_tag(group_id)
    tenant_key = hashlib.sha256(tenant_id.encode()).hexdigest()[:24]
    path = f"tenant-{tenant_key}"
    group = await admin.request("GET", f"groups/{group_id}", missing=True)
    if not isinstance(group, dict) or (
        group.get("id") != group_id
        or group.get("path") != path
        or group.get("full_path") != path
        or group.get("description") != f"ADP tenant {tenant_key}"
        or group.get("parent_id") is not None
        or group.get("shared_with_groups")
    ):
        raise RegistrationError("tenant_group_not_verified")
    for endpoint in ("members", "members/all"):
        members = await admin.pages(f"groups/{group_id}/{endpoint}")
        if not isinstance(members, list) or len(members) != 1 or any(
            not isinstance(member, dict)
            or member.get("id") != admin.owner_id
            or member.get("access_level") != 50
            for member in members
        ):
            raise RegistrationError("tenant_group_members_not_verified")
    return tag


async def verify_product_scope(admin, tenant_id, group_id, product_id, product_group_id, project_id):
    await verify_tenant_group(admin, tenant_id, group_id)
    if (not isinstance(product_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", product_id)
            or type(product_group_id) is not int or product_group_id <= 0 or product_group_id == group_id
            or type(project_id) is not int or project_id <= 0):
        raise RegistrationError("product_scope_not_verified")
    tenant_key = hashlib.sha256(tenant_id.encode()).hexdigest()[:24]
    parent_path = f"tenant-{tenant_key}"
    product_path = f"product-{product_id}"
    subgroup = await admin.request("GET", f"groups/{product_group_id}", missing=True)
    if (not isinstance(subgroup, dict) or subgroup.get("id") != product_group_id
            or subgroup.get("parent_id") != group_id or subgroup.get("path") != product_path
            or subgroup.get("full_path") != f"{parent_path}/{product_path}"
            or subgroup.get("description") != f"ADP product {product_id}"
            or subgroup.get("shared_with_groups")):
        raise RegistrationError("product_scope_not_verified")
    project = await admin.request("GET", f"projects/{project_id}", missing=True)
    if (not isinstance(project, dict) or project.get("id") != project_id
            or not isinstance(project.get("namespace"), dict)
            or project["namespace"].get("id") != product_group_id
            or project["namespace"].get("kind") != "group"
            or not isinstance(project.get("path"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", project["path"])
            or project.get("path_with_namespace") != f"{subgroup['full_path']}/{project['path']}"
            or project.get("visibility") != "private" or project.get("shared_with_groups")):
        raise RegistrationError("project_scope_not_verified")
    return project


async def enable_project_ci(admin, tenant_id, group_id, product_id, product_group_id, project_id, developer_user_id):
    if type(developer_user_id) is not int or developer_user_id <= 0 or developer_user_id == admin.owner_id:
        raise RegistrationError("developer_identity_not_verified")
    before = await verify_product_scope(admin, tenant_id, group_id, product_id, product_group_id, project_id)
    if (before.get("shared_runners_enabled") is not False
            or before.get("group_runners_enabled") not in (True, False)
            or before.get("builds_access_level") not in ("enabled", "disabled")):
        raise RegistrationError("project_ci_scope_not_verified")
    members = await admin.pages(f"projects/{project_id}/members/all")
    if (not isinstance(members, list)
            or sum(type(row.get("id")) is int and row["id"] == developer_user_id
                   and row.get("access_level") == 30 for row in members if isinstance(row, dict)) != 1
            or any(not isinstance(row, dict) or row.get("id") == developer_user_id and row.get("access_level") != 30
                   for row in members)):
        raise RegistrationError("developer_access_not_verified")
    if before["builds_access_level"] != "enabled" or before["group_runners_enabled"] is not True:
        await admin.request("PUT", f"projects/{project_id}", json={
            "builds_access_level": "enabled", "group_runners_enabled": True,
        })
    after = await verify_product_scope(admin, tenant_id, group_id, product_id, product_group_id, project_id)
    if (after.get("builds_access_level") != "enabled"
            or after.get("shared_runners_enabled") is not False
            or after.get("group_runners_enabled") is not True
            or await admin.pages(f"projects/{project_id}/members/all") != members):
        raise RegistrationError("project_ci_enablement_not_verified")
    return after


async def create_paused_runner(admin, tenant_id, group_id, tag_list, access_level, operation_id):
    tag = await verify_tenant_group(admin, tenant_id, group_id)
    if (
        not isinstance(tag_list, list)
        or not tag_list
        or tag not in tag_list
        or len(tag_list) != len(set(tag_list))
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", item) for item in tag_list)
        or any(item.startswith("adp-group-") and item != tag for item in tag_list)
        or access_level not in ("not_protected", "ref_protected")
        or not isinstance(operation_id, str)
        or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operation_id)
    ):
        raise RegistrationError("invalid_runner_policy")
    created = await admin.request(
        "POST", "user/runners", json={
            "runner_type": "group_type", "group_id": group_id,
            "run_untagged": False, "locked": True, "paused": True,
            "tag_list": tag_list, "access_level": access_level,
            "description": f"ADP runner {operation_id}",
        },
    )
    if not isinstance(created, dict) or type(created.get("id")) is not int or created["id"] <= 0:
        raise RegistrationError("runner_creation_not_verified")
    runner_id = created["id"]
    details = await admin.request("GET", f"runners/{runner_id}")
    associated = await admin.pages(f"groups/{group_id}/runners")
    if (
        not isinstance(details, dict)
        or details.get("id") != runner_id
        or details.get("runner_type") != "group_type"
        or details.get("run_untagged") is not False
        or details.get("locked") is not True
        or details.get("paused") is not True
        or details.get("access_level") != access_level
        or set(details.get("tag_list", [])) != set(tag_list)
        or details.get("description") != f"ADP runner {operation_id}"
        or not isinstance(associated, list)
        or not any(isinstance(row, dict) and row.get("id") == runner_id for row in associated)
    ):
        raise RegistrationError("runner_scope_not_verified")
    return created


class RunnerLifecycle:
    """Operate one group under an exclusive operator/workflow ownership window.

    The store persists non-secret operation phases and holds token versions. An S3
    manager integration must explicitly qualify a staged replacement to activate it.
    """

    def __init__(self, admin, store, tenant_id, group_id, tag_list, access_level):
        self.admin = admin
        self.store = store
        self.tenant_id = tenant_id
        self.group_id = group_id
        self.tag_list = tag_list
        self.access_level = access_level

    async def begin(self, operation_id, *, rotate=False):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operation_id):
            raise RegistrationError("invalid_operation_id")
        record = self.store.read_record()
        if record and record["phase"] != "finished":
            raise RegistrationError("operation_in_progress")
        await verify_tenant_group(self.admin, self.tenant_id, self.group_id)
        group_tag(self.group_id)
        current = self.store.current()
        if current and (current.get("group_id") != self.group_id or current.get("tenant_id") != self.tenant_id):
            raise RegistrationError("runner_secret_not_verified")
        if bool(current) != rotate:
            raise RegistrationError("wrong_existing_runner_state")
        record = {
            "operation_id": operation_id, "phase": "intent", "group_id": self.group_id,
            "old_runner_id": current["runner_id"] if current else None,
            "new_runner_id": None, "secret_version_id": operation_id,
            "generation": current["generation"] + 1 if current else 1,
        }
        self.store.write_record(record)
        return await self.reconcile(operation_id)

    async def _candidates(self, operation_id):
        marker = f"ADP runner {operation_id}"
        rows = await self.admin.pages("runners/all", search=marker)
        if not isinstance(rows, list):
            raise RegistrationError("runner_search_not_verified")
        associated = await self.admin.pages(f"groups/{self.group_id}/runners")
        if not isinstance(associated, list):
            raise RegistrationError("runner_scope_not_verified")
        group_ids = {row.get("id") for row in associated if isinstance(row, dict)}
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int:
                raise RegistrationError("runner_search_not_verified")
            details = await self.admin.request("GET", f"runners/{row['id']}")
            if not isinstance(details, dict):
                raise RegistrationError("runner_search_not_verified")
            if details.get("description") != marker:
                continue
            if (
                details.get("runner_type") != "group_type"
                or details.get("id") not in group_ids
                or details.get("paused") is not True
                or details.get("locked") is not True
                or details.get("run_untagged") is not False
                or details.get("access_level") != self.access_level
                or set(details.get("tag_list", [])) != set(self.tag_list)
            ):
                raise RegistrationError("runner_scope_not_verified")
            candidates.append(details["id"])
        return candidates

    async def _remove_runner(self, runner_id):
        await self.admin.request("DELETE", f"runners/{runner_id}", missing=True)
        if await self.admin.request("GET", f"runners/{runner_id}", missing=True) is not None:
            raise RegistrationError("runner_removal_not_verified")

    async def probe_revocation(self, operation_id, verify_token):
        """Revoke only a separately owned paused probe; never delete a group secret.

        Retries reconcile exact marker/group/policy matches left by an interrupted
        probe. The caller must reserve a distinct operation UUID and serialize
        the workflow. Token bytes stay in memory, including during cleanup.
        """
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operation_id):
            raise RegistrationError("invalid_operation_id")
        await verify_tenant_group(self.admin, self.tenant_id, self.group_id)
        for runner_id in await self._candidates(operation_id):
            await self._remove_runner(runner_id)
        created = None
        try:
            created = await create_paused_runner(self.admin, self.tenant_id, self.group_id,
                                                 self.tag_list, self.access_level, operation_id)
            token = created.get("token")
            if not isinstance(token, str) or not token.startswith("glrt-"):
                raise RegistrationError("runner_token_missing")
            if await verify_token(token, created["id"], operation_id) != "accepted":
                raise RegistrationError("probe_token_baseline_not_verified")
            await self._remove_runner(created["id"])
            if await verify_token(token, created["id"], operation_id) != "rejected":
                raise RegistrationError("probe_token_revocation_not_verified")
            associated = await self.admin.pages(f"groups/{self.group_id}/runners")
            if not isinstance(associated, list) or any(row.get("id") == created["id"] for row in associated):
                raise RegistrationError("runner_removal_not_verified")
            return {"runner_id": created["id"], "operation_id": operation_id,
                    "owned_probe_runner": True, "token_before": "accepted",
                    "token_after": "rejected", "runner_absent": True}
        finally:
            # Discovery handles a lost create response. Scope verification in
            # _candidates refuses modified or foreign runners before removal.
            for runner_id in await self._candidates(operation_id):
                await self._remove_runner(runner_id)

    async def reconcile(self, operation_id):
        record = self.store.read_record()
        if not record or record.get("operation_id") != operation_id or record.get("group_id") != self.group_id:
            raise RegistrationError("operation_ownership_conflict")
        if record["phase"] == "revoking":
            return await self.revoke(operation_id)
        if record["phase"] not in ("intent", "candidate"):
            return {"phase": record["phase"], "runner_id": record.get("new_runner_id")}
        staged = self.store.pending(operation_id)
        candidates = await self._candidates(operation_id)
        if staged is not None:
            if (
                not isinstance(staged, dict)
                or staged.get("version_id") != operation_id
                or staged.get("runner_id") not in candidates
                or staged.get("group_id") != self.group_id
                or staged.get("generation") != record["generation"]
            ):
                raise RegistrationError("pending_secret_not_verified")
            record["new_runner_id"] = staged["runner_id"]
        else:
            for runner_id in candidates:
                await self._remove_runner(runner_id)
            record["new_runner_id"] = None
            self.store.write_record(record)
            created = await create_paused_runner(
                self.admin, self.tenant_id, self.group_id, self.tag_list,
                self.access_level, operation_id,
            )
            runner_id = created["id"]
            record["new_runner_id"] = runner_id
            record["phase"] = "candidate"
            self.store.write_record(record)
            token = created.get("token")
            if not isinstance(token, str) or not token.startswith("glrt-"):
                raise RegistrationError("runner_token_missing")
            self.store.stage(record, token, created.get("token_expires_at"))
        record["phase"] = "staged"
        self.store.write_record(record)
        return {"phase": "staged", "runner_id": record["new_runner_id"]}

    async def qualify(self, operation_id, integration):
        import inspect

        async def resolve(value):
            return await value if inspect.isawaitable(value) else value

        record = self.store.read_record()
        if (not record or record.get("operation_id") != operation_id or record.get("group_id") != self.group_id
                or record["phase"] not in ("staged", "switching", "promoted", "qualified")):
            raise RegistrationError("operation_not_staged")
        new_id = record["new_runner_id"]
        old_id = record["old_runner_id"]
        if type(new_id) is not int or new_id <= 0 or old_id is not None and (type(old_id) is not int or old_id <= 0 or old_id == new_id):
            raise RegistrationError("operation_runner_ids_not_verified")
        if record["phase"] == "staged":
            if old_id is not None:
                await self.admin.request("PUT", f"runners/{old_id}", json={"paused": True})
                if await resolve(integration.drained(old_id)) is not True:
                    raise RegistrationError("runner_not_drained")
            record["phase"] = "switching"
            self.store.write_record(record)
        if record["phase"] == "switching":
            self.store.promote(operation_id)
            record["phase"] = "promoted"
            self.store.write_record(record)
        if record["phase"] == "promoted":
            await resolve(integration.switch(new_id))
            if await resolve(integration.manager_uses(new_id)) is not True:
                raise RegistrationError("manager_not_qualified")
            record["phase"] = "qualified"
            self.store.write_record(record)
        elif record["phase"] == "qualified":
            if await resolve(integration.manager_uses(new_id)) is not True:
                raise RegistrationError("manager_not_qualified")
        await self.admin.request("PUT", f"runners/{new_id}", json={"paused": False})
        details = await self.admin.request("GET", f"runners/{new_id}")
        if not isinstance(details, dict) or details.get("paused") is not False:
            raise RegistrationError("runner_activation_not_verified")
        if old_id is not None:
            await self._remove_runner(old_id)
        record["phase"] = "finished"
        self.store.write_record(record)
        return {"phase": "finished", "runner_id": new_id}

    async def revoke(self, operation_id):
        record = self.store.read_record()
        if record and record["phase"] == "revoking" and record["operation_id"] == operation_id:
            pass
        elif record and record["phase"] != "finished":
            raise RegistrationError("operation_in_progress")
        else:
            current = self.store.current(allow_expired=True)
            if not current or current.get("group_id") != self.group_id:
                raise RegistrationError("runner_secret_not_verified")
            record = {"operation_id": operation_id, "group_id": self.group_id,
                      "phase": "revoking", "old_runner_id": current["runner_id"]}
            self.store.write_record(record)
        runner_id = record["old_runner_id"]
        existing = await self.admin.request("GET", f"runners/{runner_id}", missing=True)
        if existing is not None:
            await self.admin.request("PUT", f"runners/{runner_id}", json={"paused": True})
            await self._remove_runner(runner_id)
        self.store.schedule_deletion()
        record["phase"] = "finished"
        self.store.write_record(record)
        return {"phase": "finished", "runner_id": runner_id}


class AwsRunnerStore:
    """SSM operation phases and Secrets Manager versions; no Terraform secret values."""

    def __init__(self, secrets, ssm, environment, tenant_id, group_id, instance_url, runner_url, tag_list, access_level, *, clock=None):
        if not isinstance(environment, str) or not re.fullmatch(r"[a-z0-9-]+", environment):
            raise RegistrationError("invalid_environment")
        group_tag(group_id)
        self.secrets = secrets
        self.clock = clock
        self.ssm = ssm
        self.secret_name = f"adp/{environment}/gitlab-runner/{group_id}"
        self.record_name = f"/adp/{environment}/gitlab-runner-operations/{group_id}"
        self.tenant_id = tenant_id
        self.group_id = group_id
        self.instance_url = instance_url
        self.runner_url = runner_url
        self.tag_list = tag_list
        self.access_level = access_level

    @staticmethod
    def _missing(error):
        return getattr(error, "response", {}).get("Error", {}).get("Code") == "ResourceNotFoundException"

    def read_record(self):
        import json

        record = json.loads(self.ssm.get_parameter(Name=self.record_name)["Parameter"]["Value"])
        if not isinstance(record, dict):
            raise RegistrationError("operation_record_invalid")
        return record or None

    def write_record(self, record):
        import json

        if not isinstance(record, dict) or set(record) - {
            "operation_id", "phase", "group_id", "generation", "old_runner_id", "new_runner_id",
            "secret_version_id",
        }:
            raise RegistrationError("operation_record_invalid")
        self.ssm.put_parameter(Name=self.record_name, Value=json.dumps(record, sort_keys=True),
                               Type="String", Overwrite=True)

    def _read_version(self, *, allow_expired=False, **kwargs):
        import json
        from datetime import datetime, timezone

        try:
            response = self.secrets.get_secret_value(SecretId=self.secret_name, **kwargs)
        except Exception as error:
            if self._missing(error):
                return None
            raise
        try:
            value = json.loads(response["SecretString"])
            if (
                type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("tenant_id") != self.tenant_id
                or type(value.get("group_id")) is not int or value["group_id"] != self.group_id
                or value.get("instance_url") != self.instance_url
                or value.get("runner_url") != self.runner_url
                or value.get("tag_list") != self.tag_list
                or value.get("access_level") != self.access_level
                or type(value.get("runner_id")) is not int or value["runner_id"] <= 0
                or type(value.get("generation")) is not int or value["generation"] <= 0
                or not isinstance(value.get("token"), str) or not value["token"]
                or response["VersionId"] != value.get("operation_id")
                or (value.get("token_expires_at") is not None and (
                    not isinstance(value["token_expires_at"], str)
                    or datetime.fromisoformat(value["token_expires_at"].replace("Z", "+00:00")).tzinfo is None
                    or (not allow_expired and datetime.fromisoformat(value["token_expires_at"].replace("Z", "+00:00")) <= (self.clock() if self.clock else datetime.now(timezone.utc)))
                ))
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise RegistrationError("runner_secret_not_verified") from None
        return {"runner_id": value["runner_id"], "group_id": value["group_id"],
                "tenant_id": value["tenant_id"], "generation": value["generation"],
                "operation_id": value["operation_id"], "version_id": response["VersionId"]}

    def current(self, *, allow_expired=False):
        return self._read_version(allow_expired=allow_expired, VersionStage="AWSCURRENT")

    def pending(self, operation_id):
        value = self._read_version(VersionId=operation_id)
        if value and value["version_id"] != operation_id:
            raise RegistrationError("pending_secret_not_verified")
        return value

    def stage(self, record, token, token_expires_at=None):
        import json

        if not isinstance(token, str) or not token.startswith("glrt-"):
            raise RegistrationError("runner_token_missing")
        value = {
            "schema_version": 1, "tenant_id": self.tenant_id, "group_id": self.group_id,
            "runner_id": record["new_runner_id"], "instance_url": self.instance_url,
            "runner_url": self.runner_url, "tag_list": self.tag_list,
            "access_level": self.access_level, "token": token,
            "token_expires_at": token_expires_at,
            "operation_id": record["operation_id"], "generation": record["generation"],
        }
        self.secrets.put_secret_value(SecretId=self.secret_name,
                                      ClientRequestToken=record["operation_id"],
                                      SecretString=json.dumps(value), VersionStages=["AWSPENDING"])

    def promote(self, operation_id):
        versions = self.secrets.describe_secret(SecretId=self.secret_name)["VersionIdsToStages"]
        if operation_id not in versions or "AWSPENDING" not in versions[operation_id] and "AWSCURRENT" not in versions[operation_id]:
            raise RegistrationError("pending_secret_not_verified")
        if "AWSCURRENT" not in versions[operation_id]:
            old = [version_id for version_id, stages in versions.items() if "AWSCURRENT" in stages]
            if len(old) > 1:
                raise RegistrationError("current_secret_not_verified")
            kwargs = {"SecretId": self.secret_name, "VersionStage": "AWSCURRENT",
                      "MoveToVersionId": operation_id}
            if old:
                kwargs["RemoveFromVersionId"] = old[0]
            self.secrets.update_secret_version_stage(**kwargs)
        current = self.current()
        if not current or current["version_id"] != operation_id:
            raise RegistrationError("secret_promotion_not_verified")

    def schedule_deletion(self):
        self.secrets.delete_secret(SecretId=self.secret_name, RecoveryWindowInDays=30)


async def execute(argv=None, *, sdk=None, admin_factory=None, clock=None):
    import argparse
    import json
    import os
    import sys
    import uuid
    from pathlib import Path

    class SafeArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            raise RegistrationError("invalid_arguments") from None

    parser = SafeArgumentParser(description="Manage a tenant-scoped GitLab runner")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--manager-config", help="Private reviewed manager integration config; only with --qualify")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--create", action="store_true")
    actions.add_argument("--rotate", action="store_true")
    actions.add_argument("--revoke", action="store_true")
    actions.add_argument("--reconcile", action="store_true")
    actions.add_argument("--qualify", action="store_true")
    actions.add_argument("--enable-ci", action="store_true")
    arguments = parser.parse_args(argv)
    operation_id = arguments.operation_id or str(uuid.uuid4())
    if (arguments.reconcile or arguments.qualify) and not arguments.operation_id:
        raise RegistrationError("operation_id_required")
    if bool(arguments.manager_config) != arguments.qualify or arguments.qualify and arguments.environment != "dev":
        raise RegistrationError("manager_config_required_for_dev_qualification_only")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operation_id):
        raise RegistrationError("invalid_operation_id")
    config_path = Path(arguments.config)
    try:
        metadata = config_path.stat()
        if config_path.is_symlink() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise RegistrationError("unsafe_operator_config")
        config = json.loads(config_path.read_text())
        required = {"tenant_id", "group_id", "tag_list", "access_level", "instance_url", "runner_url", "ca_file"}
        product_scope = {"product_id", "product_group_id", "project_id"}
        extra = set(config) - required if isinstance(config, dict) else set()
        if (not isinstance(config, dict) or not required <= set(config)
                or extra not in (set(), product_scope, product_scope | {"developer_user_id"})
                or arguments.enable_ci != (extra == product_scope | {"developer_user_id"})):
            raise RegistrationError("invalid_operator_config")
        if not isinstance(config["ca_file"], str) or not config["ca_file"]:
            raise RegistrationError("invalid_operator_config")
    except (OSError, ValueError):
        raise RegistrationError("invalid_operator_config") from None
    if sdk is None:
        import boto3
        sdk = boto3.session.Session(region_name=arguments.region)
    if admin_factory is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "gateway"))
        from src.gitlab.admin_client import GitlabAdmin
        admin_factory = GitlabAdmin
    from urllib.parse import urlsplit
    for name in ("instance_url", "runner_url"):
        url = config[name]
        try:
            invalid = (not isinstance(url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*/?", url)
                       or urlsplit(url).port == 0 or any(segment in {".", ".."} for segment in urlsplit(url).path.split("/")))
        except ValueError:
            invalid = True
        if invalid:
            raise RegistrationError("invalid_operator_url")
    group_id = config["group_id"]
    group_tag(group_id)
    secret = sdk.client("secretsmanager")
    ssm = sdk.client("ssm")
    store = AwsRunnerStore(secret, ssm, arguments.environment, config["tenant_id"], group_id,
                           config["instance_url"], config["runner_url"], config["tag_list"], config["access_level"], clock=clock)
    admin_value = secret.get_secret_value(SecretId=f"adp/{arguments.environment}/gitlab-admin-token")["SecretString"]
    if not isinstance(admin_value, str) or not admin_value.startswith("glpat-"):
        raise RegistrationError("admin_credential_not_verified")
    async with admin_factory(config["instance_url"], admin_value, ca_file=config["ca_file"]) as admin:
        if "product_group_id" in config:
            await verify_product_scope(admin, config["tenant_id"], group_id, config["product_id"],
                                       config["product_group_id"], config["project_id"])
        lifecycle = RunnerLifecycle(admin, store, config["tenant_id"], group_id,
                                    config["tag_list"], config["access_level"])
        if arguments.enable_ci:
            await enable_project_ci(admin, config["tenant_id"], group_id, config["product_id"],
                                    config["product_group_id"], config["project_id"], config["developer_user_id"])
            return {"phase": "ci_enabled", "project_id": config["project_id"]}
        if arguments.qualify:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from manager_integration import ManagerIntegration, private_json
            runner_root = Path(__file__).resolve().parents[1] / "runners"
            release = json.loads((runner_root / "versions.lock.json").read_text())
            integration = ManagerIntegration(private_json(arguments.manager_config), sdk, admin,
                                             group_id=group_id, operation_id=operation_id,
                                             region=arguments.region, runner_version=release["chart"]["runner_version"])
            try:
                result = await lifecycle.qualify(operation_id, integration)
            finally:
                integration.close()
        elif arguments.create or arguments.rotate:
            result = await lifecycle.begin(operation_id, rotate=arguments.rotate)
        elif arguments.revoke:
            result = await lifecycle.revoke(operation_id)
        else:
            result = await lifecycle.reconcile(operation_id)
    return {"phase": result["phase"], "operation_id": operation_id, "runner_id": result.get("runner_id"),
            "secret_version_id": operation_id if result["phase"] == "staged" else None}


def main(argv=None, *, sdk=None, admin_factory=None, clock=None):
    import asyncio
    import json
    import logging
    import sys

    previous = logging.root.manager.disable
    try:
        logging.disable(max(previous, logging.CRITICAL))
        result = asyncio.run(execute(argv, sdk=sdk, admin_factory=admin_factory, clock=clock))
    except RegistrationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("runner_provider_unavailable", file=sys.stderr)
        return 3
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
