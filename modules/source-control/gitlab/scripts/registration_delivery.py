"""Validate protected, run-bound registration probe receipts; never run probes."""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

LIVE_CHECKS = frozenset({
    "registration-product-group", "registration-qualification-groups", "ca-root-verified",
    "group-token-scope", "runner-revocation", "instance-no-unreviewed-change",
})


def private_receipt(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 65536):
        raise ValueError("private registration receipt required")
    return json.loads(path.read_text())


def target_binding(env):
    """Targets come from reviewed protected configuration, independently of observations."""
    targets = json.loads(env["ADP_REGISTRATION_TARGETS_JSON"])
    ids = {"tenant_parent", "product_subgroup", "project", "qualification_parent"}
    hashes = {"approved_root_sha256", "reviewed_plan_sha256"}
    if (not isinstance(targets, dict) or set(targets) != ids | hashes
            or any(type(targets[key]) is not int or targets[key] <= 0 for key in ids)
            or len({targets[key] for key in ids - {"project"}}) != 3
            or any(not isinstance(targets[key], str) or not re.fullmatch(r"[0-9a-f]{64}", targets[key]) for key in hashes)):
        raise ValueError("reviewed registration targets required")
    canonical = json.dumps(targets, sort_keys=True, separators=(",", ":")).encode()
    return targets, hashlib.sha256(canonical).hexdigest()


def expected_observations(targets):
    return {
        "registration-product-group": {
            "tenant_parent": targets["tenant_parent"], "product_subgroup": targets["product_subgroup"],
            "project": targets["project"], "tenant_members_verified": True,
            "product_namespace_verified": True, "group_runner_type": "group_type",
            "runner_paused": True, "shared_runners": False,
        },
        "registration-qualification-groups": {
            "qualification_parent": targets["qualification_parent"],
            "tenant_members_verified": True, "distinct_parent": True,
            "group_runner_type": "group_type", "runner_paused": True,
            "cross_group_assignment": False,
        },
        "ca-root-verified": {
            "approved_root_sha256": targets["approved_root_sha256"], "https_liveness": 200,
            "chain_verified": True, "stored_bundle_readback_verified": True,
        },
        "group-token-scope": {
            "group_bound_secrets_verified": True, "runner_group_assignment_verified": True,
            "run_untagged": False, "locked": True,
            "admin_secret_policy_verified": True, "token_secret_policies_verified": True,
        },
        "runner-revocation": {
            "owned_probe_runner": True, "token_before": "accepted",
            "token_after": "rejected", "runner_absent": True,
        },
        "instance-no-unreviewed-change": {
            "reviewed_plan_sha256": targets["reviewed_plan_sha256"],
            "applied_plan_matches_review": True, "post_apply_zero_change_plan": True,
        },
    }


def verify(receipt, env, account, checkout):
    """Return versioned references only after the exact accepted live checks agree."""
    targets, targets_sha = target_binding(env)
    binding = {
        "schema_version": 1, "source_revision": checkout,
        "workflow_run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "account_id": account, "region": env["AWS_REGION"],
        "correlation": env["ADP_CORRELATION"], "targets_sha256": targets_sha,
    }
    if (not isinstance(receipt, dict) or set(receipt) != set(binding) | {"checks"}
            or any(type(receipt[key]) is not type(value) or receipt[key] != value for key, value in binding.items())):
        raise ValueError("registration receipt binding invalid")
    checks = receipt["checks"]
    if not isinstance(checks, dict) or set(checks) != LIVE_CHECKS:
        raise ValueError("six live registration checks required")
    refs = {}
    for name, required in expected_observations(targets).items():
        row = checks[name]
        if not isinstance(row, dict) or set(row) != {"verdict", "observed", "evidence_ref"}:
            raise ValueError("live registration check incomplete")
        observed = row["observed"]
        if (row["verdict"] != "pass" or not isinstance(observed, dict)
                or set(observed) != set(required)
                or any(type(observed[key]) is not type(value) or observed[key] != value
                       for key, value in required.items())):
            raise ValueError("live registration oracle did not pass")
        ref = row["evidence_ref"]
        # Exact run/attempt/check path prevents a substring of a stale run from
        # qualifying. These references identify retained private probe readbacks.
        if (not isinstance(ref, str) or not re.fullmatch(
                r"s3://[a-z0-9][a-z0-9.-]{2,62}/[a-zA-Z0-9/_=.\-]+\?versionId=[a-zA-Z0-9_.~%-]{1,256}", ref)
                or f"/runs/{binding['workflow_run_id']}/attempts/{binding['run_attempt']}/{name}.json?versionId=" not in ref):
            raise ValueError("versioned live evidence reference required")
        refs[name] = ref
    if len(set(refs.values())) != len(refs):
        raise ValueError("distinct live evidence references required")
    return refs


def check_reviewed_instance_plan(plan, targets, instance_id, *, instance_gate=False):
    """Refuse replayed creates, IAM changes and ungated stateful instance updates.

    The digest is over the canonical terraform-show JSON (not a plan generated
    after dispatch). It must be compared before any terraform apply. The gate
    argument must come from a verified deployment gate, never a workflow input.
    """
    if (not isinstance(plan, dict) or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id)
            or not isinstance(targets, dict)):
        raise ValueError("reviewed instance plan required")
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != targets.get("reviewed_plan_sha256"):
        raise ValueError("instance plan differs from reviewed plan")
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        raise ValueError("saved plan resource changes required")
    for row in changes:
        if not isinstance(row, dict) or not isinstance(row.get("change"), dict):
            raise ValueError("invalid saved plan change")
        change = row["change"]
        if change.get("actions") == ["no-op"] or row.get("mode") == "data" and change.get("actions") == ["read"]:
            continue
        if (change.get("actions") != ["update"] or row.get("type") != "aws_instance"
                or not instance_gate or not isinstance(change.get("before"), dict)
                or not isinstance(change.get("after"), dict)
                or change["before"].get("id") != instance_id or change["after"].get("id") != instance_id):
            raise ValueError("unreviewed or ungated instance/prerequisite change")
        before, after = change["before"], change["after"]
        allowed = {"user_data", "user_data_base64"}
        if (set(before) != set(after) or not any(before.get(key) != after.get(key) for key in allowed)
                or any(before[key] != after[key] for key in before if key not in allowed)
                or any(value for key, value in change.get("after_unknown", {}).items() if key not in allowed)):
            raise ValueError("instance change exceeds reviewed user_data update")
    return True


async def observe_registration_groups(admin, config, targets):
    """Read live admin API for both owned groups; no fixture-derived verdicts.

    The registrar handles reconciliation/creation separately. This readback
    refuses wrong runner IDs, cross-parent assignment and shared project runners.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    source = Path(__file__).with_name("register-runner.py")
    spec = importlib.util.spec_from_loader("registration_live_registrar", SourceFileLoader("registration_live_registrar", str(source)))
    registrar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registrar)
    product, qualification = config["product"], config["qualification"]
    if (product["tenant_id"] == qualification["tenant_id"]
            or product["group_id"] != targets["tenant_parent"]
            or product["product_group_id"] != targets["product_subgroup"]
            or product["project_id"] != targets["project"]
            or qualification["group_id"] != targets["qualification_parent"]):
        raise ValueError("reviewed registration targets mismatch")
    project = await registrar.verify_product_scope(admin, product["tenant_id"], targets["tenant_parent"],
                                                   product["product_id"], targets["product_subgroup"], targets["project"])
    await registrar.verify_tenant_group(admin, qualification["tenant_id"], targets["qualification_parent"])
    if project.get("shared_runners_enabled") is not False:
        raise ValueError("shared project runners enabled")
    for name, group_id, other_id in (("product", targets["tenant_parent"], targets["qualification_parent"]),
                                     ("qualification", targets["qualification_parent"], targets["tenant_parent"])):
        item = config[name]
        runner_id = item["runner_id"]
        operation_id = item.get("operation_id")
        if (type(runner_id) is not int or runner_id <= 0 or not isinstance(operation_id, str)
                or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operation_id)):
            raise ValueError("owned runner operation required")
        detail = await admin.request("GET", f"runners/{runner_id}", missing=True)
        owned = await admin.pages(f"groups/{group_id}/runners")
        foreign = await admin.pages(f"groups/{other_id}/runners")
        if (not isinstance(detail, dict) or detail.get("id") != runner_id
                or detail.get("runner_type") != "group_type" or detail.get("paused") is not True
                or detail.get("locked") is not True or detail.get("run_untagged") is not False
                or detail.get("description") != f"ADP runner {item['operation_id']}"
                or f"adp-group-{group_id}" not in detail.get("tag_list", [])
                or not isinstance(owned, list) or not any(isinstance(row, dict) and row.get("id") == runner_id for row in owned)
                or not isinstance(foreign, list) or any(isinstance(row, dict) and row.get("id") == runner_id for row in foreign)):
            raise ValueError("runner parent isolation not verified")
    expected = expected_observations(targets)
    return {name: expected[name] for name in ("registration-product-group", "registration-qualification-groups")}


def wait_for_private_liveness(client, url, *, timeout=600, clock=time.monotonic, pause=time.sleep):
    """Allow the reviewed stop/start to finish booting, retaining strict TLS."""
    import httpx

    deadline = clock() + timeout
    while True:
        try:
            response = client.get(url)
        except (httpx.ConnectError, httpx.TimeoutException):
            response = None
        if response is not None:
            if response.status_code == 200:
                if response.json() != {"status": "ok"}:
                    raise ValueError("private HTTPS liveness not verified")
                return
            if response.status_code not in {500, 502, 503, 504}:
                raise ValueError("private HTTPS liveness not verified")
        remaining = deadline - clock()
        if remaining <= 0:
            raise ValueError("private HTTPS liveness not verified after startup deadline")
        pause(min(5, remaining))


def observe_ca_root(acm, secrets, targets, *, certificate_arn, runner_url, root_file, ca_secret_arn, ca_version):
    """Recheck independent root, TLS liveness and the stored bundle without writing a secret.

    Call with the separately verified CA-exporter identity, not the registrar.
    No PEM, API body or private token is included in the returned observation.
    """
    import importlib.util
    import ssl
    from importlib.machinery import SourceFileLoader
    from urllib.parse import urlsplit

    import httpx

    source = Path(__file__).with_name("export-runner-ca.py")
    spec = importlib.util.spec_from_loader("registration_live_ca", SourceFileLoader("registration_live_ca", str(source)))
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    parsed = urlsplit(runner_url)
    certificate = re.fullmatch(r"arn:aws:acm:([a-z]{2}-[a-z]+-[0-9]+):([0-9]{12}):certificate/[0-9a-f-]{36}", certificate_arn)
    secret = re.fullmatch(r"arn:aws:secretsmanager:([a-z]{2}-[a-z]+-[0-9]+):([0-9]{12}):secret:adp/([a-z0-9-]+)/gitlab/ca-chain-[A-Za-z0-9]{6}", ca_secret_arn)
    if (parsed.scheme != "https" or not parsed.hostname or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)
            or not isinstance(ca_version, str) or not ca_version or not certificate or not secret
            or certificate.group(1, 2) != secret.group(1, 2)):
        raise ValueError("private CA target required")
    document = acm.get_certificate(CertificateArn=certificate_arn)
    bundle = exporter.validate_chain(document["Certificate"], document.get("CertificateChain"),
                                     root_file, targets["approved_root_sha256"], parsed.hostname)
    saved = secrets.get_secret_value(SecretId=ca_secret_arn, VersionId=ca_version)
    if saved.get("VersionId") != ca_version or saved.get("SecretString") != bundle:
        raise ValueError("stored CA bundle differs from approved chain")
    context = ssl.create_default_context(cadata=bundle)
    with httpx.Client(verify=context, follow_redirects=False, trust_env=False, timeout=5) as client:
        wait_for_private_liveness(client, runner_url.rstrip("/") + "/-/liveness")
    return expected_observations(targets)["ca-root-verified"]
