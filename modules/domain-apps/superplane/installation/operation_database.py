"""Explicit, reviewable shared-operation database preparation entry point.

Run as ``python -m installation.operation_database`` from the domain module.
Default mode saves a local plan. Execution uses the selected installer identity,
its installation lock, a pinned paid-worker image and an isolated temporary pod.
"""

import argparse
import copy
import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .cluster_probe import ClusterProbe
from .config import MODULE, digest, load, require, validate
from .database_preparation import owned_secret
from .runner import Installer, local_lock

COMPONENT = "superplane-paid-worker"


class OperationInstaller(Installer):
    @property
    def image_components(self):
        components = super().image_components
        return (
            components
            if COMPONENT in components
            else (*components[:-1], COMPONENT, components[-1])
        )


def preparation_plan(installer, schema):
    import re

    env = installer.env
    require(
        isinstance(schema, str)
        and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema)
        and not schema.startswith("pg_")
        and schema
        not in {
            "public",
            "information_schema",
            env["database"]["schema"],
            env["database"]["skypilot_schema"],
        },
        "Shared operations require a distinct dedicated schema",
    )
    prefix = "superplane_" + env["environment"].replace("-", "_")
    roles = {kind: prefix + "_ops_" + kind for kind in ("owner", "gateway", "worker")}
    require(
        all(len(role) <= 63 for role in roles.values()),
        "Operation role name exceeds PostgreSQL limit",
    )
    require(
        env.get("image_execution") == "cluster",
        "Shared preparation requires private cluster execution",
    )
    source = installer.lock.get("image_sources", {}).get(COMPONENT, {})
    require(
        source.get("source_revision") == installer.lock["source_revision"]
        and source.get("repository") == "adp-superplane-paid-worker"
        and source.get("registry")
        == f"{env['account_id']}.dkr.ecr.{env['region']}.amazonaws.com"
        and re.fullmatch(
            r"sha256:[a-f0-9]{64}", installer.lock.get("images", {}).get(COMPONENT, "")
        ),
        "Exact reviewed paid-worker image required",
    )
    paths = [
        Path(__file__),
        Path(__file__).with_name("operation_database_probe.py"),
        MODULE.parents[1] / "harness/jobs/harness_jobs/installation.py",
        MODULE.parents[1] / "harness/jobs/harness_jobs/schema.py",
    ]
    plan = {
        "version": 1,
        "mode": "shared-operation-database",
        "installation_id": installer.owner,
        "environment_sha256": digest(env),
        "release_sha256": installer.release,
        "source_sha256": digest(
            {
                str(p.relative_to(MODULE.parents[2])): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in paths
            }
        ),
        "database": env["database"]["database"],
        "schema": schema,
        "roles": roles,
        "forbidden_roles": [
            f"superplane_{env['environment']}_{kind}"
            for kind in ("runtime", "migration", "skypilot")
        ],
        "secrets": {
            kind: f"adp/{env['environment']}/superplane/{kind}-database"
            for kind in ("operation", "operation-worker", "domain")
        },
        "worker_secret": "superplane-paid-worker-db",
        "forbidden_schemas": [
            env["database"]["schema"],
            env["database"]["skypilot_schema"],
        ],
        "backup_id": env["database"]["backup_id"],
    }
    plan["marker"] = "adp-harness-installation-v1:" + digest(
        {k: plan[k] for k in ("installation_id", "database", "schema", "roles")}
    )
    if env.get("paid_worker"):
        require(
            env["paid_worker"].get("operation_schema") == schema
            and env["paid_worker"].get("database_secret") == plan["worker_secret"],
            "Paid worker must consume the exact prepared schema and Secret",
        )
    return plan


def secret_value(installer, name, factory):
    installer.verify_deployment_identity()
    proxy = copy.copy(installer)
    proxy.env = dict(installer.env, secrets={"operation": name})
    return owned_secret(proxy, "operation", factory)


def execute(installer, plan, approved, admin_url):
    require(approved == digest(plan), "Approve the exact saved shared database plan")
    require(
        bool(admin_url),
        "Set SUPERPLANE_DATABASE_ADMIN_URL for this one-shot operator process",
    )
    installer.target()
    installer.images()
    with installer.exclusive():
        env = installer.env
        instance = installer.json(
            installer.aws(
                "rds",
                "describe-db-instances",
                "--db-instance-identifier",
                env["database"]["identifier"],
            )
        )["DBInstances"][0]
        backup = installer.json(
            installer.aws(
                "rds",
                "describe-db-snapshots",
                "--db-snapshot-identifier",
                plan["backup_id"],
            )
        )["DBSnapshots"][0]
        require(
            backup["Status"] == "available"
            and backup["DBInstanceIdentifier"] == env["database"]["identifier"]
            and instance.get("DbiResourceId")
            and backup.get("DbiResourceId") == instance["DbiResourceId"],
            "Backup does not identify selected database",
        )
        endpoint = instance["Endpoint"]
        target = {"host": endpoint["Address"], "port": endpoint["Port"]}
        parsed = urlsplit(admin_url)
        require(
            parsed.scheme == "postgresql"
            and parsed.hostname == target["host"]
            and (parsed.port or 5432) == target["port"]
            and parsed.path == "/" + plan["database"]
            and not parsed.query
            and not parsed.fragment,
            "Administrator targets another database",
        )
        stored = installer.json(
            installer.aws(
                "secretsmanager",
                "describe-secret",
                "--secret-id",
                env["secrets"]["database"],
            )
        )
        require(
            {"Key": "SuperplaneInstallation", "Value": installer.owner}
            in stored.get("Tags", []),
            "Domain credentials belong to another installation",
        )
        domain = installer.json(
            installer.aws(
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                env["secrets"]["database"],
            )
        )
        domain_value = json.loads(domain["SecretString"])
        dsn = domain_value["runtime-url"]
        parsed = urlsplit(dsn)
        require(
            parsed.scheme == "postgresql"
            and parsed.hostname == target["host"]
            and (parsed.port or 5432) == target["port"]
            and parsed.path == "/" + plan["database"]
            and unquote(parsed.username or "") == plan["forbidden_roles"][0]
            and bool(parsed.password)
            and not parsed.query
            and not parsed.fragment,
            "Domain runtime credential identity differs",
        )
        passwords, dsns, versions = {}, {}, {}
        for purpose, role_kind in (
            ("operation", "gateway"),
            ("operation-worker", "worker"),
        ):
            role = plan["roles"][role_kind]

            def make_value(role=role):
                password = secrets.token_urlsafe(40)
                return {
                    "dsn": f"postgresql://{role}:{quote(password, safe='')}@{target['host']}:{target['port']}/{plan['database']}"
                }

            value, versions[purpose] = secret_value(
                installer, plan["secrets"][purpose], make_value
            )
            require(set(value) == {"dsn"}, "Operation secret has unexpected fields")
            parsed = urlsplit(value["dsn"])
            require(
                parsed.scheme == "postgresql"
                and parsed.hostname == target["host"]
                and (parsed.port or 5432) == target["port"]
                and parsed.path == "/" + plan["database"]
                and unquote(parsed.username or "") == role
                and bool(parsed.password)
                and not parsed.query
                and not parsed.fragment,
                "Operation credential identity differs",
            )
            passwords[role], dsns[role] = unquote(parsed.password), value["dsn"]
        request = {
            "database": plan["database"],
            "schema": plan["schema"],
            "owner_role": plan["roles"]["owner"],
            "runtime_roles": [plan["roles"]["gateway"], plan["roles"]["worker"]],
            "forbidden_roles": plan["forbidden_roles"],
            "forbidden_schemas": plan["forbidden_schemas"],
            "marker": plan["marker"],
        }
        installer.receipt["operation_secret_versions"] = versions
        installer.save()
        with ClusterProbe(installer) as probe:
            addresses = installer.resolve_database_addresses(endpoint)
            probe.isolate(
                database_cidrs=[a + ("/128" if ":" in a else "/32") for a in addresses],
                database_port=target["port"],
            )
            result = probe.run(
                COMPONENT,
                [
                    "python",
                    "-c",
                    Path(__file__).with_name("operation_database_probe.py").read_text(),
                ],
                values={
                    "ADP_OPERATION_DATABASE_INPUT": json.dumps(
                        {
                            "request": request,
                            "passwords": passwords,
                            "runtime_dsns": dsns,
                            "admin_url": admin_url,
                            "target": target,
                            "ca_pem": domain_value["ca-pem"],
                        }
                    )
                },
            )
            observed = installer.json(result)
            require(
                result.returncode == 0
                and observed.get("status") == "prepared"
                and observed.get("roles_authenticated") is True
                and observed.get("schema") == plan["schema"],
                "Shared database preparation did not authenticate its isolated runtime roles",
            )
        value, versions["domain"] = secret_value(
            installer, plan["secrets"]["domain"], lambda: {"dsn": dsn}
        )
        require(
            value == {"dsn": dsn},
            "Existing domain projection differs; explicit rotation is required",
        )
        # A create-only Secret is projected after real database authentication.
        # Same-input reconciliation compares all content and the owner label.
        from .config import LABEL

        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": plan["worker_secret"],
                "namespace": env["namespace"],
                "labels": {LABEL: installer.owner},
            },
            "type": "Opaque",
            "stringData": {
                "domain-dsn": dsn,
                "execution-dsn": dsns[plan["roles"]["worker"]],
                "ca.pem": domain_value["ca-pem"],
            },
        }
        existing = installer.kube(
            "-n",
            env["namespace"],
            "get",
            "secret",
            plan["worker_secret"],
            "--ignore-not-found",
            "-o",
            "json",
        )
        if existing.stdout.strip():
            import base64

            current = installer.json(existing)
            require(
                current["metadata"].get("labels", {}).get(LABEL) == installer.owner
                and current.get("type") == "Opaque"
                and {
                    k: base64.b64decode(v).decode()
                    for k, v in current.get("data", {}).items()
                }
                == secret["stringData"],
                "Existing worker database Secret differs; replacement refused",
            )
        else:
            installer.kube("create", "-f", "-", data=json.dumps(secret))
        installer.receipt["operation_database"] = dict(
            observed,
            secret_versions=versions,
            domain_secret_version=domain["VersionId"],
        )
        installer.receipt["status"] = "shared-database-prepared"
        installer.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("environment", "release-lock", "output"):
        parser.add_argument("--" + arg, required=True, type=Path)
    parser.add_argument("--schema", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--recover-lock", action="store_true")
    parser.add_argument("--confirm-stopped")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args(argv)
    try:
        env, lock = load(args.environment), load(args.release_lock)
        validate(env, lock, control_plane_only=True)
        with local_lock(args.output):
            installer = OperationInstaller(
                env, lock, args.output.resolve(), control_plane_only=True
            )
            plan = preparation_plan(installer, args.schema)
            if installer.receipt_path.exists():
                previous = load(installer.receipt_path)
                require(
                    args.resume
                    and previous.get("mode") == "shared-operation-database"
                    and previous.get("operation_database_plan") == plan,
                    "Use --resume with the exact original shared database plan",
                )
                require(
                    args.recover_lock
                    or (
                        not previous.get("remote_lock")
                        and not previous.get("lock_attempt")
                        and not previous.get("temporary_preflight", {}).get(
                            "cleanup_required"
                        )
                    ),
                    "Recover the recorded installer lock/temporary namespace before resuming",
                )
                if previous.get("status") == "shared-database-prepared":
                    import re

                    require(
                        previous.get("installation_id") == installer.owner
                        and digest(previous.get("environment")) == digest(installer.env)
                        and digest(previous.get("release_lock")) == installer.release
                        and re.fullmatch(r"[a-f0-9]{16}", previous.get("run_id", "")),
                        "Completed shared preparation receipt identity differs",
                    )
                    installer.run_id, installer.receipt = previous["run_id"], previous
                else:
                    installer.resume(previous)
            else:
                require(
                    not args.resume and not args.recover_lock,
                    "Resume/recovery requires the existing receipt",
                )
            installer.receipt.update(
                mode="shared-operation-database",
                operation_database_plan=plan,
                plan_sha256=digest(plan),
            )
            installer.save()
            if args.recover_lock:
                installer.recover_lock(args.confirm_stopped)
            elif args.execute:
                installer.phase(
                    "shared-operation-database",
                    lambda: execute(
                        installer,
                        plan,
                        args.approved_plan_sha256,
                        os.environ.get("SUPERPLANE_DATABASE_ADMIN_URL", ""),
                    ),
                )
            print(
                json.dumps(
                    {
                        "status": installer.receipt["status"],
                        "plan_sha256": digest(plan),
                        "receipt": str(installer.receipt_path),
                    }
                )
            )
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "status": "refused",
                    "reason": "Shared operation database preparation refused; inspect the private plan and receipt",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
