#!/usr/bin/env python3
"""Installed Product CLI journey; release hashes and API reads are independent."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import common
from capability_contrast import _write_session
from product_profile_proxy import drop_product_response


class RecordingCli(common.Cli):
    def __init__(self, binary, env, transcript, evidence, actor):
        super().__init__(binary, env, transcript, timeout=60)
        self.evidence = evidence
        self.actor = actor

    def run(self, args, **kwargs):
        diagnostics = {}
        code, payload = super().run(args, diagnostics=diagnostics, **kwargs)
        self.evidence.setdefault("cli_events", []).append(
            {
                "actor": self.actor,
                "argv": ["adp", *args, "--json"],
                "exit_code": code,
                "output": payload,
                "stdout_diagnostic": diagnostics.get("stdout"),
                "stderr_diagnostic": diagnostics.get("stderr"),
            }
        )
        return code, payload


def detail(cli, arguments):
    code, envelope = cli.run(["product", *arguments])
    common.require(
        code == 0
        and isinstance(envelope, dict)
        and envelope.get("status") == "ok"
        and isinstance(envelope.get("detail"), dict),
        "Installed Product command did not return an OK JSON profile",
    )
    return envelope["detail"]


def verify_install(config):
    binary = Path(config["cli_path"])
    expected = config.get("expected_hashes") or {}
    common.require(
        binary.is_file() and binary.name == "adp",
        "The installed Product CLI is missing",
    )
    common.require(
        expected.get("adp") and expected.get("adp-product.py"),
        "Pinned CLI helper checksums are missing",
    )
    installed = common.hashes_of(binary.parent)
    common.require(
        all(
            installed.get(name) == digest
            for name, digest in expected.items()
            if name != "install.sh"
        ),
        "Installed CLI differs from the independently pinned release",
    )
    return {name: installed[name] for name in ("adp", "adp-product.py")}


def run_roundtrip(config, cli, readback, evidence):
    fixture = config.get("product_profile") or {}
    common.require(
        fixture.get("owned_mutations_authorized") is True,
        "Run-owned Product mutation authority missing",
    )
    common.require(
        fixture.get("tenant_id") == config.get("org_id")
        and fixture.get("login_user_id") == config.get("test_user_id"),
        "Product fixture does not match the authenticated owner and tenant",
    )
    common.require(
        config.get("evaluation_id"), "Run-owned Product evaluation identity missing"
    )
    evidence["installed_hashes"] = verify_install(config)
    name = "Owned Product " + config["evaluation_id"]
    created = detail(
        cli,
        [
            "create",
            "--name",
            name,
            "--purpose",
            "Installed CLI verification",
            "--intended-user",
            "Evaluation owner",
            "--starting-point",
            "idea",
            "--new-project",
            "--idempotency-key",
            "create_" + config["evaluation_id"],
        ],
    )
    product_id, project_id = created.get("product_id"), created.get("project_id")
    common.require(
        product_id and project_id, "Product create returned no owned identity"
    )
    evidence["owned_product"] = {"product_id": product_id, "project_id": project_id}
    scope = ["--product", product_id, "--project", project_id]
    # A mounted app path, appended by `common.api` to the approved gateway base,
    # which already carries the external `/api` prefix CloudFront strips. The
    # installed `adp-product.py` addresses `/v1/products/...` with the same base,
    # so spelling `/api` here read back `<base>/api/api/v1/products/...`: the live
    # 404 that would have been compared against the CLI's own answer. The
    # `/api/admin/identity/...` routes `common.api` sends elsewhere really are
    # mounted under `/api` and are deliberately left alone.
    path = f"/v1/products/{product_id}/projects/{project_id}"

    def compare(profile, revision, *, archived):
        status, observed = readback(path)
        common.require(
            status == 200
            and profile == observed
            and profile.get("product_id") == product_id
            and profile.get("project_id") == project_id
            and profile.get("tenant_id") == fixture["tenant_id"]
            and profile.get("revision") == revision
            and profile.get("product", {}).get("name") == name
            and ("archived_at" in profile) == archived,
            "CLI profile disagrees with independently read Product state",
        )
        evidence.setdefault("api_readback", []).append(
            {
                "revision": revision,
                "product_id": product_id,
                "project_id": project_id,
                "archived": archived,
                "status": status,
            }
        )

    compare(created, 1, archived=False)
    shown = detail(cli, ["show", *scope])
    compare(shown, 1, archived=False)
    listed = detail(cli, ["list", "--limit", "100"])
    common.require(
        any(item.get("id") == product_id for item in listed.get("items", [])),
        "Owned Product absent from installed CLI list",
    )
    with tempfile.TemporaryDirectory(prefix="adp-product-update-") as directory:
        patch = Path(directory) / "patch.json"
        patch.write_text(
            json.dumps({"purpose": "Installed CLI readback verified"}), encoding="utf-8"
        )
        updated = detail(
            cli,
            [
                "update",
                *scope,
                "--input",
                str(patch),
                "--revision",
                "1",
                "--idempotency-key",
                "update_" + config["evaluation_id"],
            ],
        )
    status, observed = readback(path)
    common.require(
        status == 200
        and updated == observed
        and updated.get("product", {}).get("purpose")
        == "Installed CLI readback verified",
        "Product update disagrees with independent API readback",
    )
    compare(updated, 2, archived=False)
    for action, revision in (("archive", 3), ("restore", 4)):
        profile = detail(
            cli,
            [
                "lifecycle",
                *scope,
                "--action",
                action,
                "--reason",
                "Run-owned CLI check",
                "--yes",
                "--revision",
                str(revision - 1),
                "--idempotency-key",
                action + "_" + config["evaluation_id"],
            ],
        )
        compare(profile, revision, archived=action == "archive")
    evidence["result"] = "passed"
    evidence["success"] = True


def execute(config, evidence):
    if config.get("mode") == "denial_retry":
        execute_denial_retry(config, evidence)
        return
    common.require(config.get("mode") == "roundtrip", "Unknown Product journey mode")
    session = common.session_tokens(config)
    with tempfile.TemporaryDirectory(prefix="adp-owned-product-") as directory:
        home = Path(directory)
        env = common.clean_env(config, HOME=home, ADP_TENANT=config["org_id"])
        _write_session(home, config["gateway_url"], session)
        cli = RecordingCli(
            config["cli_path"], env, evidence["transcript"], evidence, "owner"
        )
        run_roundtrip(
            config,
            cli,
            lambda path: common.api(config, path, session["access_token"]),
            evidence,
        )


def failure(cli, arguments, exit_code, error_code, evidence):
    code, envelope = cli.run(["product", *arguments], expected=None)
    common.require(
        code == exit_code
        and isinstance(envelope, dict)
        and envelope.get("status") == "failed"
        and isinstance(envelope.get("error"), dict)
        and envelope["error"].get("code") == error_code,
        f"Installed Product denial did not return {error_code}/{exit_code}",
    )
    evidence.setdefault("denials", []).append(
        {"command": arguments[0], "exit_code": code, "error_code": error_code}
    )
    return envelope


def run_denial_retry(config, cli, foreign_cli, readback, fault_client, evidence):
    fixture = config.get("product_profile") or {}
    owned = config.get("owned_product") or {}
    common.require(
        fixture.get("owned_mutations_authorized") is True
        and fixture.get("tenant_id") == config.get("org_id")
        and fixture.get("login_user_id") == config.get("test_user_id")
        and fixture.get("foreign_tenant_id") != fixture.get("tenant_id")
        and fixture.get("foreign_tenant_id"),
        "Verified distinct Product owner and foreign fixture required",
    )
    common.require(
        owned.get("product_id") and owned.get("project_id"),
        "An earlier run-owned Product is required; no new Product will be created for denial checks",
    )
    evidence["installed_hashes"] = verify_install(config)
    product_id, project_id = owned["product_id"], owned["project_id"]
    # The mounted app path, appended to the approved gateway base by
    # `common.api` exactly as in the roundtrip above. It is also what the fault
    # relay is handed, but the relay must NOT match it literally: the installed
    # CLI resolves its own configured base to one ending in `/api` before it
    # sends anything, so the committed request reaches the relay spelled
    # `/api/v1/products/...`. That translation belongs to the relay
    # (`product_profile_proxy.CLI_MOUNT`); matching the bare app path there is
    # what delivered the response this case requires to be dropped.
    path = f"/v1/products/{product_id}/projects/{project_id}"
    status, baseline = readback(path)
    common.require(
        status == 200
        and baseline.get("product_id") == product_id
        and baseline.get("project_id") == project_id
        and baseline.get("tenant_id") == fixture["tenant_id"]
        and baseline.get("product", {}).get("name")
        == "Owned Product " + config["evaluation_id"]
        and type(baseline.get("revision")) is int
        and baseline["revision"] > 1,
        "Pre-existing Product was not verified as run-owned before denial checks",
    )
    scope = ["--product", product_id, "--project", project_id]
    with tempfile.TemporaryDirectory(prefix="adp-product-denials-") as directory:
        patch = Path(directory) / "patch.json"
        patch.write_text(json.dumps({"purpose": "Retry reconciled"}), encoding="utf-8")
        stale = [
            "update",
            *scope,
            "--input",
            str(patch),
            "--revision",
            str(baseline["revision"] - 1),
            "--idempotency-key",
            "stale_" + config["evaluation_id"],
        ]
        failure(cli, stale, 4, "stale_revision", evidence)
        foreign_show = failure(foreign_cli, ["show", *scope], 5, "not_found", evidence)
        foreign_update = failure(
            foreign_cli,
            [
                "update",
                *scope,
                "--input",
                str(patch),
                "--revision",
                str(baseline["revision"]),
                "--idempotency-key",
                "foreign_" + config["evaluation_id"],
            ],
            5,
            "not_found",
            evidence,
        )
        common.require(
            product_id not in json.dumps(foreign_show) + json.dumps(foreign_update),
            "Foreign Product denial disclosed run-owned identity",
        )
        list_code, listing = foreign_cli.run(["product", "list"], expected=None)
        common.require(
            list_code == 0
            and isinstance(listing, dict)
            and listing.get("status") == "ok"
            and isinstance(listing.get("detail", {}).get("items"), list)
            and all(
                item.get("id") != product_id for item in listing["detail"]["items"]
            ),
            "Foreign Product list disclosed a run-owned Product",
        )
        common.require(
            readback(path) == (200, baseline),
            "Denied Product commands changed durable state",
        )
        key = "retry_" + config["evaluation_id"]
        retry = [
            "update",
            *scope,
            "--input",
            str(patch),
            "--revision",
            str(baseline["revision"]),
            "--idempotency-key",
            key,
        ]
        code, envelope, dropped = fault_client(retry, path, key)
        common.require(
            dropped
            and code == 4
            and isinstance(envelope, dict)
            and envelope.get("error", {}).get("code") == "unknown_mutation_outcome",
            "Committed Product response was not dropped and reported as unknown",
        )
        status, committed = readback(path)
        common.require(
            status == 200
            and committed.get("revision") == baseline["revision"] + 1
            and committed.get("product", {}).get("purpose") == "Retry reconciled",
            "Ambiguous Product update did not commit exactly once",
        )
        replayed = detail(cli, retry)
        common.require(
            replayed == committed and readback(path) == (200, committed),
            "Exact Product retry created another write or changed the readback",
        )
    evidence["retry"] = {
        "initial_revision": baseline["revision"],
        "committed_revision": committed["revision"],
        "dropped_exit_code": code,
        "dropped_error_code": "unknown_mutation_outcome",
        "replay_exit_code": 0,
    }
    evidence["result"] = "passed"
    evidence["success"] = True


def execute_denial_retry(config, evidence):
    fixture = config.get("product_profile") or {}
    work_dir = Path(config["work_dir"])
    foreign_ref = str(work_dir.parent / "session.json")
    common.require(
        work_dir.name == "product-actor"
        and fixture.get("foreign_session_ref") == foreign_ref
        and fixture.get("foreign_tenant_id")
        and fixture.get("foreign_user_id")
        and fixture["foreign_tenant_id"] != config.get("org_id"),
        "A distinct run-owned foreign Product session reference is required",
    )
    admin = common.session_tokens(config)
    foreign = common.session_tokens(
        {**config, "work_dir": str(work_dir.parent), "session_ref": foreign_ref}
    )
    _status, identity = common.api(
        config, "/auth/cli/admin-session", foreign["access_token"]
    )
    common.require(
        identity.get("verified") is True
        and identity.get("org_id") == fixture["foreign_tenant_id"]
        and identity.get("user_id") == fixture.get("foreign_user_id")
        and fixture["foreign_user_id"] != config.get("test_user_id"),
        "Protected foreign session identity or tenant did not match its binding",
    )
    with tempfile.TemporaryDirectory(prefix="adp-owned-product-denial-") as directory:
        home = Path(directory)
        owner_home = home / "owner"
        foreign_home = home / "foreign"
        owner_home.mkdir(mode=0o700)
        foreign_home.mkdir(mode=0o700)
        _write_session(owner_home, config["gateway_url"], admin)
        _write_session(foreign_home, config["gateway_url"], foreign)
        owner = RecordingCli(
            config["cli_path"],
            common.clean_env(config, HOME=owner_home, ADP_TENANT=fixture["tenant_id"]),
            evidence["transcript"],
            evidence,
            "owner",
        )
        other = RecordingCli(
            config["cli_path"],
            common.clean_env(
                config, HOME=foreign_home, ADP_TENANT=fixture["foreign_tenant_id"]
            ),
            evidence["transcript"],
            evidence,
            "foreign",
        )

        def fault_client(arguments, path, key):
            with drop_product_response(config["gateway_url"], path, key) as (
                gateway,
                state,
            ):
                _write_session(owner_home, gateway, admin)
                try:
                    code, envelope = owner.run(["product", *arguments], expected=None)
                finally:
                    _write_session(owner_home, config["gateway_url"], admin)
            return code, envelope, state["dropped"]

        run_denial_retry(
            config,
            owner,
            other,
            lambda path: common.api(config, path, admin["access_token"]),
            fault_client,
            evidence,
        )
