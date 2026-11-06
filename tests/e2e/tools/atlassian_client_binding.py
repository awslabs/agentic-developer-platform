"""Materialize isolated client ledgers and existing MCP-grant bindings, never tokens."""

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from tests.e2e.tools.atlassian_client_matrix import (
    CLIENTS,
    HANDLE,
    load_isolated_fixtures,
)
from tests.e2e.tools.atlassian_readback_grant import IdentityReadbackGrant
from tests.e2e.tools.atlassian_setup_binding import OIDC_SCRIPT


def bind_clients(raw, *, directory, account, region, reader_role):
    if len(raw.encode()) > 8192:
        raise ValueError("Protected client manifest exceeds bound")
    manifest = json.loads(raw)
    if (
        type(manifest) is not dict
        or set(manifest)
        not in (
            {"run_id", "owners", "readback"},
            {"run_id", "owners", "readback", "cleanup"},
        )
        or not isinstance(manifest["run_id"], str)
        or not HANDLE.fullmatch(manifest["run_id"])
        or type(manifest["owners"]) is not dict
        or set(manifest["owners"]) != CLIENTS
        or type(manifest["readback"]) is not dict
        or set(manifest["readback"]) != CLIENTS
    ):
        raise ValueError("Protected two-client MCP bindings are required")
    grants = [
        IdentityReadbackGrant(manifest["readback"][client], manifest["owners"][client])
        for client in sorted(CLIENTS)
    ]
    if (
        len({grant.owner_id for grant in grants}) != len(CLIENTS)
        or len({grant.config["provider_account_id"] for grant in grants})
        != len(CLIENTS)
        or any(
            grant.config["account"] != account
            or grant.config["region"] != region
            or grant.config["adapter_role_arn"] != reader_role
            for grant in grants
        )
        or not re.fullmatch(r"[0-9]{12}", account)
    ):
        raise ValueError("Client owner or independent reader authority differs")
    if "cleanup" in manifest:
        from tests.e2e.tools.atlassian_fixture_provider import JiraFixtureProvider

        if type(manifest["cleanup"]) is not dict or set(manifest["cleanup"]) != CLIENTS:
            raise ValueError("Separate cleanup capability for both clients is required")
        for client in sorted(CLIENTS):
            JiraFixtureProvider(
                manifest["cleanup"][client],
                readback=manifest["readback"][client],
                owner_id=manifest["owners"][client],
                run_id=manifest["run_id"],
            )
    root = Path(tempfile.mkdtemp(prefix="atlassian-clients-", dir=directory))
    try:
        for client in sorted(CLIENTS):
            (root / client).mkdir(mode=0o700)
        process = shlex.join(
            [
                sys.executable,
                str(OIDC_SCRIPT.resolve(strict=True)),
                reader_role,
                grants[0].config["environment"],
            ]
        )
        files = {
            "clients.json": json.dumps(manifest),
            "aws-credentials": "",
            "aws-config": f"[profile oidc-adapter]\nregion = {region}\ncredential_process = {process}\n",
        }
        for name, content in files.items():
            descriptor = os.open(
                root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return root / "clients.json", root / "aws-config"
    except Exception:
        shutil.rmtree(root)
        raise


def main():
    path = None
    previous = os.environ.get("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE")
    try:
        path, config = bind_clients(
            os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_JSON"],
            directory=os.environ["RUNNER_TEMP"],
            account=os.environ["EXPECTED_ACCOUNT"],
            region=os.environ["AWS_REGION"],
            reader_role=os.environ["ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN"],
        )
        os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"] = str(config)
        fixtures = load_isolated_fixtures(path)
        native = None
        if not os.environ.get("ADP_TOOLS_ATLASSIAN_RECOVERY_RESUME_JSON") and (
            os.environ.get("ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_JSON")
            or os.environ.get("ADP_TOOLS_ATLASSIAN_NATIVE_SESSIONS_JSON")
        ):
            native = bind_native_plans(
                path.parent,
                os.environ["ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_JSON"],
                os.environ["ADP_TOOLS_ATLASSIAN_NATIVE_SESSIONS_JSON"],
                fixtures,
            )
        with open(os.environ["GITHUB_ENV"], "a") as stream:
            stream.write(
                f"ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE={path}\nADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE={config}\n"
            )
            if native is not None:
                stream.write(f"ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_FILE={native}\n")
    except Exception:
        if path is not None:
            shutil.rmtree(path.parent)
        raise SystemExit("Protected independent client readback unavailable") from None
    finally:
        if previous is None:
            os.environ.pop("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE", None)
        else:
            os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"] = previous


def bind_native_plans(root, raw, sessions, fixtures):
    from tests.e2e.tools.atlassian_native_matrix import validate_plan
    from modules.tools.platform.scripts.native_test_session import (
        ApprovedReference,
        JWT,
    )

    if len(raw.encode()) > 131072 or len(sessions.encode()) > 32768:
        raise ValueError("Native plan or sessions exceed their private bound")
    plans, tokens = json.loads(raw), json.loads(sessions)
    if (
        type(plans) is not dict
        or set(plans) != CLIENTS
        or type(tokens) is not dict
        or set(tokens) != CLIENTS
        or any(
            not isinstance(token, str) or len(token) > 16384 or not JWT.fullmatch(token)
            for token in tokens.values()
        )
        or len(set(tokens.values())) != len(CLIENTS)
    ):
        raise ValueError("Two distinct original native sessions are required")
    references = set()
    for client, plan in plans.items():
        if type(plan) is not dict or set(plan) != {
            "approval",
            "model",
            "bounds",
            "cells",
        }:
            raise ValueError("Native plan contains unsupported configuration")
        approval = plan.pop("approval")
        plan.update(
            approval_file=str(root / client / "native-approval.json"),
            login_file=str(root / client / "native-login.token"),
        )
        validate_plan(plan, fixtures[client], client)
        for key, value in (
            ("approval_file", json.dumps(approval)),
            ("login_file", tokens[client]),
        ):
            descriptor = os.open(
                plan[key], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        reference = ApprovedReference.read(plan["approval_file"], os.environ)
        references.add(reference.run_id)
    if len(references) != len(CLIENTS):
        raise ValueError("Each owner needs its own original native approval")
    destination = root / "native-plans.json"
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(plans, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return destination


if __name__ == "__main__":
    main()
