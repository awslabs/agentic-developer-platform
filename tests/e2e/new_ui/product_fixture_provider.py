"""Protected browser fixtures using durable ownership and the V01 retirement API."""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from tests.e2e.chat.helpers import TestCredentials, get_cognito_tokens
from tests.e2e.new_ui.product_fixture_bootstrap import (
    preflight,
    preflight_b03,
    read_configuration,
)
from tests.e2e.new_ui.product_fixture_ports import (
    Membership,
    ProtectedRetirement,
    checked_json,
)
from tests.e2e.new_ui.product_fixture_state import BrowserRunState, FixtureStore
from tests.e2e.new_ui.product_live_release import (
    selection_from_environment,
    verified_frontend_release,
)
from tests.e2e.new_ui.product_recovery import ProductRun
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.product_onboarding.product_v02_record import private_json


class PrivateSession(dict):
    def __repr__(self):
        return "<protected Product fixture session>"


class BrowserFixtureProvider:
    def __init__(self, request, base_url, control, *, state, retire, frontend, evidence_context,
                 credentials, recover=False, authenticate=None):
        authenticate = authenticate or get_cognito_tokens
        self.request, self.base_url, self.state, self.retire = request, base_url, state, retire
        self.binding = state.binding
        self.run_id = self.binding["run_id"]
        self.frontend, self.evidence_context = frontend, evidence_context
        self.scope_inputs = None
        self.membership = Membership(
            request, base_url, control, self.binding,
            expected_team_id=self.binding["editor_team_id"] if self.binding.get("scope_fixture_mode") == "connected_scope" else "",
        )
        admin = checked_json(request.get(base_url + "/api/auth/cli/admin-session",
                                        headers=self.headers(control), timeout=30_000))
        if admin.get("verified") is not True or admin.get("role") != "platform_admin":
            raise ValueError("fixture control session is not a verified administrator")
        self.usage_control = control
        tenant = checked_json(request.get(
            base_url + "/api/api/admin/identity/organizations/" + quote(self.binding["tenant_id"], safe=""),
            headers=self.headers(control), timeout=30_000,
        ))
        connected_scope = self.binding.get("scope_fixture_mode") == "connected_scope"
        if (tenant.get("id") != self.binding["tenant_id"] or not isinstance(tenant.get("name"), str)
                or not tenant["name"] or not (connected_scope or tenant["name"].startswith("eval-regression-"))):
            raise ValueError("browser fixture is not an isolated evaluation workspace")
        if connected_scope and not all(
            credentials[peer]["username"].startswith("eval-regression-") for peer in ("owner", "editor")
        ):
            raise ValueError("connected scope requires dedicated ordinary fixture logins")
        peers = [
            ("owner", self.binding["actor_id"], self.binding["owner_subject"], self.binding["tenant_id"]),
            ("nonmember", self.binding["nonmember_id"], self.binding["nonmember_subject"], self.binding["nonmember_tenant_id"]),
        ]
        if "editor_id" in self.binding:
            peers.append(("editor", self.binding["editor_id"], self.binding["editor_subject"], self.binding["tenant_id"]))
        for label, actor, subject, tenant_id in peers:
            listing = checked_json(request.get(
                base_url + "/api/api/admin/identity/organizations/" + quote(tenant_id, safe="") + "/users",
                headers=self.headers(control), timeout=30_000,
            ))
            users = listing.get("users")
            if (not isinstance(users, list) or len(users) > (1000 if connected_scope else 100)
                    or (connected_scope and tenant_id == self.binding["tenant_id"] and listing.get("total") != len(users))
                    or (not connected_scope and tenant_id == self.binding["tenant_id"]
                        and len(users) != (2 if "editor_id" in self.binding else 1))):
                raise ValueError("fixture identity inventory is not bounded and isolated")
            matching = [user for user in users if user.get("id") == actor and user.get("org_id") == tenant_id
                        and user.get("cognito_sub") == subject and user.get("cognito_username") == credentials[label]["username"]
                        and user.get("role") == "member"]
            if len(matching) != 1:
                raise ValueError("fixture login does not map to its ordinary canonical actor")
        self.editor_membership = None
        if "editor_id" in self.binding:
            self.editor_membership = Membership(
                request, base_url, control, {**self.binding, "actor_id": self.binding["editor_id"]},
                baseline_key="scope_editor_membership", restore_key="scope_editor_restore_required",
                expected_team_id=self.binding["editor_team_id"],
            )
        if recover:
            state.claim(recover=True)
            if self.editor_membership is not None:
                from .product_scope_actors import restore_scope_owner_role

                restore_scope_owner_role(self)
                self.editor_membership.restore(state)
            self.membership.restore(state)
        baseline = self.membership.baseline()
        editor_baseline = self.editor_membership.baseline() if self.editor_membership is not None else None
        self.owner = PrivateSession(authenticate(TestCredentials(**credentials["owner"])))
        self.observer = PrivateSession(authenticate(TestCredentials(**credentials["owner"])))
        self.nonmember = PrivateSession(authenticate(TestCredentials(**credentials["nonmember"])))
        self.editor = PrivateSession(authenticate(TestCredentials(**credentials["editor"]))) if editor_baseline else None
        sessions = [
            (self.owner, self.binding["actor_id"], self.binding["owner_subject"], self.binding["tenant_id"]),
            (self.observer, self.binding["actor_id"], self.binding["owner_subject"], self.binding["tenant_id"]),
            (self.nonmember, self.binding["nonmember_id"], self.binding["nonmember_subject"], self.binding["nonmember_tenant_id"]),
        ]
        if self.editor is not None:
            sessions.append((self.editor, self.binding["editor_id"], self.binding["editor_subject"], self.binding["tenant_id"]))
        for tokens, actor, subject, tenant_id in sessions:
            identity = checked_json(request.get(base_url + "/api/auth/me", headers=self.headers(tokens), timeout=30_000))
            if (identity.get("account_type") != "human" or identity.get("is_admin") is not False
                    or identity.get("user_id") not in {actor, subject} or identity.get("org_id") != tenant_id):
                raise ValueError("authenticated peer differs from the ordinary protected fixture")
            tokens.update(tenant_id=tenant_id, actor_id=actor)
        if not recover:
            state.claim()
        if "membership" not in state.document and not state.document["cases"]:
            state.save({**state.document, "membership": baseline})
        elif Membership.shape(baseline) != Membership.shape(state.document["membership"]):
            raise ValueError("recovery fixture membership differs from the original baseline")
        if editor_baseline is not None:
            if "scope_editor_membership" not in state.document and not state.document["cases"]:
                state.save({**state.document, "scope_editor_membership": editor_baseline,
                            "scope_editor_restore_required": False})
            elif Membership.shape(editor_baseline) != Membership.shape(state.document["scope_editor_membership"]):
                raise ValueError("scope editor membership differs from its original baseline")

    @staticmethod
    def headers(tokens):
        return {"Authorization": "Bearer " + tokens["access_token"]}

    def snapshot(self):
        result, cursor, seen = {}, None, set()
        for _ in range(5):
            suffix = "?cursor=" + quote(cursor, safe="") if cursor else ""
            page = checked_json(self.request.get(self.base_url + "/api/v1/products" + suffix,
                                                headers=self.headers(self.observer), timeout=10_000))
            if not isinstance(page.get("items"), list) or len(page["items"]) > 100:
                raise ValueError("fixture discovery exceeded its bound")
            for item in page["items"]:
                if item["id"] in result:
                    raise ValueError("fixture discovery repeated a Product")
                result[item["id"]] = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            cursor = page.get("next_cursor")
            if cursor is None:
                return result
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("fixture discovery returned an invalid cursor")
            seen.add(cursor)
        raise RuntimeError("fixture discovery exceeded five pages")

    def protected_retire(self, case, manifest):
        stored = self.state.document["cases"][case]
        if stored.get("manifest") != manifest:
            raise ValueError("retirement does not match durable ownership")
        receipt = self.retire(manifest)
        if self.snapshot() != stored["before"]:
            raise RuntimeError("other Product discovery changed during fixture execution")
        return {**receipt, "shared_unchanged": True}

    def run(self, case, document):
        # Issue #8250: the B03 scope case owns the plain run-named Product. The
        # reviewed V01 create guard in product_recovery.ProductRun accepts a
        # fixed set of case suffixes and "-scope" is not one of them, so the
        # scope case keeps the unsuffixed name instead of relaxing that guard.
        return ProductRun(
            FixtureStore(self.state, case), lambda manifest: self.protected_retire(case, manifest),
            run_id=self.run_id, tenant_id=self.binding["tenant_id"], actor_id=self.binding["actor_id"],
            source_revision=self.binding["source_revision"], owner_token=self.owner["access_token"], request_body=document["request"],
            name_suffix="-" + case if case in ("resume_ui", "resume_cli", "progress", "recovery",
                                               "guidance_idea", "guidance_existing_assets", "guidance_running_product",
                                               "v11_idea", "v11_existing_assets", "v11_running_product", "v11_resume", "v11_release") else "",
        )

    def cleanup(self, case, request):
        if any(self.state.document.get(key, {}).get("cleanup_pending") for key in ("catalog", "catalog_history", "catalog_recovery")):
            raise ValueError("catalog links must close before Product retirement")
        scope = self.state.document.get("scope", {})
        if scope.get("cleanup_pending"):
            from tests.e2e.product_onboarding.product_v04_history import restore_history

            from .product_scope_cleanup import prepare_scope_retirement

            restore_history(self)
            prepare_scope_retirement(self, case)
        if self.editor_membership is not None:
            from .product_scope_actors import restore_scope_owner_role

            restore_scope_owner_role(self)
            self.editor_membership.restore(self.state)
        self.membership.restore(self.state)
        store = FixtureStore(self.state, case)
        document, manifest = store.load(self.run_id)
        if document is not None:
            if self.binding.get("scope_fixture_mode") == "connected_scope" and scope.get("case") != case:
                expected = {"scope_fixture_mode": "connected_scope", "fixture_editor_id": self.binding["editor_id"]}
                if manifest.get("scope") is not None or any(manifest.get(key) not in (None, value) for key, value in expected.items()):
                    raise ValueError("Single-Project retirement differs from its protected fixture identity")
                # Persist the exact membership proof before the retirement
                # call, so a lost response recovers the same owned intent.
                store.save(self.run_id, document, {**manifest, **expected})
            cleanup = self.run(case, document).retire(request, self.base_url, self.observer)
            if scope.get("cleanup_pending"):
                from .product_scope_cleanup import confirm_scope_retirement

                confirm_scope_retirement(self, cleanup)

        else:
            if self.snapshot() != self.state.document["cases"][case]["before"]:
                raise RuntimeError("fixture discovery changed without a persisted intent")
            cleanup = {"run_id": self.run_id, "no_mutation": True}
        value = deepcopy(self.state.document)
        value["cases"][case]["cleanup"] = cleanup
        self.state.save(value)
        return cleanup

    @contextmanager
    def fixture(self, case, record):
        guidance_cases = {"guidance_idea": "idea", "guidance_existing_assets": "existing_assets",
                          "guidance_running_product": "running_product",
                          "v11_idea": "idea", "v11_existing_assets": "existing_assets",
                          "v11_running_product": "running_product"}
        if case not in ("create", "conflict", "evidence", "resume_ui", "resume_cli", "progress", "recovery", "v11_resume", "v11_release", "scope", *guidance_cases) or case in self.state.document["cases"]:
            raise ValueError("recover the original case instead of creating another browser intent")
        value = deepcopy(self.state.document)
        value["cases"][case] = {"before": self.snapshot(), "document": None, "manifest": None}
        self.state.save(value)
        fixture = {
            **self.binding, "owner": self.owner, "observer": self.observer, "nonmember": self.nonmember,
            "revoked": self.owner, "frontend_release": self.frontend, "state_store": FixtureStore(self.state, case),
            "evidence_context": self.evidence_context,
            "protected_cleanup": lambda manifest: self.protected_retire(case, manifest),
            "revoke": lambda: self.membership.revoke(self.state),
        }
        try:
            if case != "create":
                name = "eval-product-" + self.run_id + ("-" + case if case in ("resume_ui", "resume_cli", "progress", "recovery", "v11_resume", "v11_release", *guidance_cases) else "")
                body = {"name": name, "purpose": "Verify saved Product workspace information",
                        "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}}
                run = self.run(case, {"request": body})
                key = uuid4().hex
                run.record_intent(body, key)
                fixture.update(run.reconcile(self.request, self.base_url), intent_key=key)
                if case in guidance_cases and guidance_cases[case] != "idea":
                    scope = (f"/api/v1/products/{quote(fixture['product_id'], safe='')}/projects/"
                             f"{quote(fixture['project_id'], safe='')}")
                    current = checked_json(record.request(self.request).get(
                        self.base_url + scope, headers=self.headers(self.observer), timeout=30_000))
                    updated = checked_json(record.request(self.request).patch(
                        self.base_url + scope, headers={**self.headers(self.owner),
                            "Idempotency-Key": uuid4().hex, "If-Profile-Revision": str(current["revision"])},
                        data={"starting_point": guidance_cases[case]}, timeout=30_000))
                    if updated["revision"] != current["revision"] + 1 or updated["product"]["starting_point"] != guidance_cases[case]:
                        raise ValueError("Guided fixture starting point was not saved")
            yield fixture
        finally:
            receipt = self.cleanup(case, record.request(self.request))
            if case == "recovery":
                from .product_session_recovery import assert_cleanup_receipt

                assert_cleanup_receipt(receipt, fixture)
            record.cleanup.append(receipt)

    def recover(self):
        if self.editor_membership is not None:
            from .product_scope_actors import restore_scope_owner_role

            restore_scope_owner_role(self)
            self.editor_membership.restore(self.state)
        self.membership.restore(self.state)
        receipts = {case: self.cleanup(case, self.request) for case in self.state.document["cases"]}
        memberships = {}
        for label, membership in (("owner", self.membership), ("editor", self.editor_membership)):
            if membership is None:
                continue
            observed = membership.baseline()
            baseline = self.state.document[membership.baseline_key]
            if Membership.shape(observed) != Membership.shape(baseline):
                raise RuntimeError("fixture membership baseline was not independently restored")
            memberships[label] = {"baseline": baseline, "observed": observed}
        lock = self.state.release()
        return {"run_id": self.run_id, "suite_id": self.state.namespace,
                "source_revision": self.binding["source_revision"], "cases": receipts,
                "memberships": memberships, "ownership_lock": lock}


PAIRING_IDENTITY = (
    "source_revision", "component", "run_id", "run_attempt", "workflow_revision", "workflow_path",
    "region", "product_profile_enabled", "product_onboarding_enabled", "product_profile_schema_version",
)


def pairing_differences(artifact, expected):
    """Name every field in which the signed API release is not the pairing this run selected.

    Issue #8246: the pairing comparison was a single ``any(...)`` raising one opaque message, so a
    maintained ``backend_release`` naming an older API release surfaced as identical setup errors
    that named neither the differing field nor either revision, and the actual mismatch could only
    be established by reading protected secret metadata out of band. Reporting the difference
    weakens no check - the pairing must still be exact - and the fixture owner is told precisely
    what to maintain. Values are quoted only for the release identity above; account and repository
    identifiers are reported as differing without echoing them.
    """
    differences = []
    for key, value in expected.items():
        actual = getattr(artifact, key)
        if actual == value:
            continue
        if key in PAIRING_IDENTITY:
            differences.append(f"{key}={actual!r}, this run requires {value!r}")
        else:
            differences.append(f"{key} differs from this run's target")
    return differences


def deployment_context(selection, frontend, config, session, request, base_url, directory, *, onboarding_enabled=None):
    from src.orchestration.deployment_runtime_contract import ReleaseArtifact
    from src.orchestration.deployment_runtime_reader import (
        KubernetesRuntime,
        PilotRuntimeReader,
    )

    from modules.gateway.deployment_evidence.store import EvidenceStore

    selected = config["backend_release"]
    source = selection["source_revision"]
    for revision in (config["plan_revision"], config["requirements_revision"], selected["workflow_revision"]):
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("fixture release and requirements revisions must be pinned")
    signed = EvidenceStore().read(
        account=frontend["release"]["account_id"], environment=selection["environment"], region=selection["region"],
        repository=selection["repository"], repository_id=selection["repository_id"], run_id=selected["run_id"],
        attempt=selected["attempt"], kind="release", name="gateway-backend",
        workflow_path=".github/workflows/gateway-deploy.yml", workflow_revision=selected["workflow_revision"],
    )
    identity = (
        f"the maintained backend_release names gateway-deploy run {selected['run_id']} attempt "
        f"{selected['attempt']} at workflow revision {selected['workflow_revision']}"
    )
    if signed is None:
        raise ValueError(
            "signed Product backend release is missing: " + identity
            + ", and the evidence store holds no signed gateway-backend release for it; this run selected "
            + f"source {source}. The maintained pairing must name the API release deployed for this source."
        )
    payload, digest, reference = signed
    artifact = ReleaseArtifact.model_validate_json(payload)
    expected = {"component": "gateway-backend", "account_id": frontend["release"]["account_id"],
                "region": selection["region"], "run_id": selected["run_id"], "run_attempt": selected["attempt"],
                "workflow_revision": selected["workflow_revision"], "repository_id": selection["repository_id"],
                "workflow_path": ".github/workflows/gateway-deploy.yml", "product_profile_enabled": True,
                "product_profile_schema_version": "1.0"}
    # Product lifecycle/scope checks work with either admission setting. An
    # onboarding suite explicitly requires True; an off-state suite may require
    # False. In every case, the independently observed flag must match the signed
    # release below. Absence of a requirement is not an off-state requirement.
    if onboarding_enabled is not None:
        if type(onboarding_enabled) is not bool:
            raise ValueError("Onboarding admission requirement must be a boolean")
        expected["product_onboarding_enabled"] = onboarding_enabled
    differences = pairing_differences(artifact, expected)
    if differences:
        raise ValueError(
            "Product backend release differs from the selected fixture source: " + identity
            + f", while this run selected source {source}; differing fields: " + "; ".join(differences)
        )
    from tests.e2e.product_onboarding.product_completion_observe import (
        compatible_source,
    )

    compatible_source(artifact.source_revision, source, "gateway-backend")
    cluster, namespace = artifact.resource_id.split("/")
    description = session.client("eks").describe_cluster(name=cluster)["cluster"]
    reader = PilotRuntimeReader(kubernetes=partial(KubernetesRuntime, proof_invoke_url=config["internal_invoke_url"]))
    observed = reader.inspect(session, description, namespace, artifact=artifact, artifact_hash=digest,
                              evidence_ref=reference, environment=selection["environment"])
    if observed.product_onboarding_enabled is not artifact.product_onboarding_enabled:
        raise ValueError("Observed onboarding admission differs from the selected release")
    check_served_assets(request, base_url, frontend, source)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    observation_ref = private_json(directory / "product-runtime-observation.json", observed.model_dump(mode="json"))
    identities = {
        "requirements_revision": config["requirements_revision"], "source_revision": source,
        "api_workflow_revision": artifact.workflow_revision, "frontend_workflow_revision": frontend["release"]["workflow_revision"],
        "schema_head": observed.migration_head, "api_image_digest": observed.image_digest,
        "frontend_release_sha256": frontend["digest"], "frontend_release_ref": frontend["reference"],
        "cli_checksum": observed.cli_manifest_sha256,
    }
    return {"plan_revision": config["plan_revision"], "identities": identities,
            "deployment": {"receipt_ref": reference, "observation_ref": observation_ref,
            "observed": {**identities, "api_source_revision": artifact.source_revision,
                         "frontend_source_revision": frontend["release"]["source_revision"],
                         "product_profile_enabled": observed.product_profile_enabled,
                         "product_onboarding_enabled": observed.product_onboarding_enabled}},
            "cli": {"status": "artifact_only", "artifact_kind": "command-manifest", "source_revision": source,
                    "cli_checksum": observed.cli_manifest_sha256, "artifact_ref": reference}}


def build_provider(environment, base_url, request, control, *, recovery_run_id=None, onboarding_enabled=None, scope_editor_enabled=False):
    import boto3

    selection = selection_from_environment(environment)
    session = boto3.Session(region_name=selection["region"])
    frontend = verified_frontend_release(selection=selection, base_url=base_url, sts=session.client("sts"),
                                         ssm=session.client("ssm"), cloudfront=session.client("cloudfront"))
    config = read_configuration(session.client("secretsmanager"), selection["environment"],
                                scope_editor_enabled=scope_editor_enabled and not recovery_run_id)
    # A connected evaluation workspace keeps both dedicated actors on its
    # protected team, including for onboarding and recovery suites. Carry that
    # identity contract from configuration even when this suite does not create
    # multiple delivery scopes; otherwise its owner is checked against the
    # unrelated isolated-workspace membership baseline.
    scope_editor_enabled = scope_editor_enabled or config.get("scope_fixture_mode") == "connected_scope"
    run_id = recovery_run_id or f"product-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}"
    if scope_editor_enabled and not recovery_run_id:
        preflight_b03(config, base_url=base_url, environment=environment)
    if config.get("base_url") != base_url or not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id):
        raise ValueError("protected fixture configuration targets another origin or run")
    # Issue #8246: the documented configuration was never published, so a
    # missing or partial one surfaced as a raw client error or a KeyError in the
    # middle of fixture construction. For a FRESH B02 run, prove the private
    # contents are complete and usable and that the protected API release
    # pairing is the pairing selected for this run, before the claim, any
    # membership change or any browser. Read-only.
    #
    # Recovery is excluded deliberately: it constructs the provider in order to
    # restore a temporarily revoked owner, so an ordinary-baseline expectation
    # here would block the mandatory recovery path. The B03 and V11 inventories
    # keep their own fixture contracts and extra protected sections.
    if not (recovery_run_id or scope_editor_enabled or onboarding_enabled):
        preflight(config, base_url=base_url, environment=environment)
    binding = {key: config[key] for key in ("tenant_id", "actor_id", "owner_subject", "nonmember_id", "nonmember_subject", "nonmember_tenant_id")}
    if (not all(isinstance(value, str) and value for value in binding.values())
            or binding["tenant_id"] == binding["nonmember_tenant_id"] or binding["actor_id"] == binding["nonmember_id"]
            or binding["owner_subject"] == binding["nonmember_subject"]):
        raise ValueError("protected browser peers must be distinct ordinary identities")
    binding.update(run_id=run_id, source_revision=selection["source_revision"], base_url=base_url,
                   retirement_role=config["retirement_role"], internal_invoke_url=config["internal_invoke_url"])
    if scope_editor_enabled:
        from tests.e2e.new_ui.product_scope_fixture import _inputs_valid

        scope_inputs = config.get("scope_inputs")
        if not recovery_run_id:
            _inputs_valid(scope_inputs)
        editor = config.get("scope_editor")
        if (not isinstance(editor, dict) or set(editor) != {"actor_id", "subject", "team_id"}
                or any(not isinstance(value, str) or not value for value in editor.values())
                or editor["actor_id"] in {binding["actor_id"], binding["nonmember_id"]}
                or editor["subject"] in {binding["owner_subject"], binding["nonmember_subject"]}
                or not isinstance(config.get("credentials", {}).get("editor"), dict)):
            raise ValueError("B03 requires a distinct protected ordinary editor and team")
        if config.get("scope_fixture_mode") == "connected_scope":
            binding["scope_fixture_mode"] = "connected_scope"
        binding.update(editor_id=editor["actor_id"], editor_subject=editor["subject"],
                       editor_team_id=editor["team_id"])
    state = BrowserRunState(session.client("s3"), config["state_bucket"], config["state_kms_key_id"],
                            binding, namespace=environment.get("PRODUCT_SUITE_ID") or None)
    account = frontend["release"]["account_id"]
    role = config["retirement_role"]
    if not re.fullmatch(rf"arn:aws:iam::{re.escape(account)}:role/[A-Za-z0-9_+=,.@/-]+", role) or role == selection["checks_role"]:
        raise ValueError("protected retirement role must be separate and in the selected account")
    assumed = session.client("sts").assume_role(RoleArn=role, RoleSessionName=run_id, DurationSeconds=3600)["Credentials"]
    retirement_session = boto3.Session(
        aws_access_key_id=assumed["AccessKeyId"], aws_secret_access_key=assumed["SecretAccessKey"],
        aws_session_token=assumed["SessionToken"], region_name=selection["region"],
    )
    retire = ProtectedRetirement(request, retirement_session, account=account, region=selection["region"], role_arn=role,
                                checks_role=selection["checks_role"], origin=config["internal_invoke_url"])
    context = {} if recovery_run_id else deployment_context(selection, frontend, config, session, request, base_url, Path("test-results").resolve(), onboarding_enabled=onboarding_enabled)
    provider = BrowserFixtureProvider(
        request, base_url, control, state=state, retire=retire, frontend=frontend, evidence_context=context,
        credentials=config["credentials"], recover=bool(recovery_run_id),
    )
    provider.scope_inputs = scope_inputs if scope_editor_enabled and not recovery_run_id else None
    return provider


def main():
    from playwright.sync_api import sync_playwright

    from tests.e2e.chat.helpers import CLOUDFRONT_URL, fetch_test_credentials

    run_id = os.environ.get("PRODUCT_RECOVERY_RUN_ID") or f"product-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    control = PrivateSession(get_cognito_tokens(fetch_test_credentials()))
    with sync_playwright() as playwright:
        request = playwright.request.new_context()
        try:
            provider = build_provider(os.environ, CLOUDFRONT_URL.rstrip("/"), request, control, recovery_run_id=run_id,
                                      scope_editor_enabled=os.environ.get("PRODUCT_B03_MODE") == "true")
            receipt = provider.recover()
            path = Path("test-results").resolve()
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_json(path / ("product-recovery-" + run_id + ".json"), receipt)
        finally:
            request.dispose()


if __name__ == "__main__":
    main()
