"""Read-only preflight for the protected Product browser fixture configuration.

Issue #8246: AWS readback established that the documented private entry
`adp/<environment>/gateway/product-browser-fixtures` existed with no version at
all, so `GetSecretValue` on AWSCURRENT could only raise, and the documented
setup had never been published or made repeatable. (The first correlated
frontend deployment was cancelled before release publication and before any
browser executed, so no browser attempt was observed; the missing configuration
was the established prerequisite failure.) Without this module an unpublished or
partial configuration surfaces as a raw client error or a `KeyError` on a
documented key, in the middle of fixture construction, with secret *metadata*
existence as the only operator-visible signal.

This module is the explicit statement of required contents, evaluated for a
fresh B02 run before the fixture claim, before any membership change and before
any browser starts:

* `read_configuration` names the maintained entry, the refused operation and the
  exact documented keys when no usable version is published.
* `validate_configuration` accepts only a complete, well-formed documented
  configuration: this origin, two distinct ordinary people (distinct workspaces,
  actor rows *and* Cognito subjects), complete private credentials per peer and
  immutable commit revisions.
* `check_release_pairing` keeps source/release pairing dynamic. A repaired source
  must be qualified only against the API release actually deployed for it, so the
  protected pairing has to equal the pairing selected for the run; no run
  identity is pinned in portable code.

It is read-only by construction: it creates, deletes and mutates nothing - no
user, workspace, membership, password or secret version - and needs no authority
beyond the browser checks identity's existing `GetSecretValue`. Fixture
ownership, run claims, interrupted-run recovery, live identity/login proof and
always-run teardown remain with the existing durable provider, which keeps its
own validation for the recovery, B03 and V11 contracts.
"""

from __future__ import annotations

import json
import re

SECRET_NAME = "adp/{environment}/gateway/product-browser-fixtures"
TARGET_KEYS = ("base_url", "state_bucket", "state_kms_key_id", "retirement_role", "internal_invoke_url")
IDENTITY_KEYS = (
    "tenant_id",
    "actor_id",
    "owner_subject",
    "nonmember_id",
    "nonmember_subject",
    "nonmember_tenant_id",
)
RELEASE_KEYS = ("plan_revision", "requirements_revision", "backend_release")
CREDENTIAL_KEYS = ("username", "password", "cognito_user_pool_id", "cognito_client_id")
PEERS = ("owner", "nonmember")
DOCUMENTED_KEYS = TARGET_KEYS + IDENTITY_KEYS + RELEASE_KEYS + ("credentials",)
REVISION = re.compile(r"[a-f0-9]{40}")
MAX_CONFIG = 65536


def read_configuration(secretsmanager, environment, *, scope_editor_enabled=False):
    """Read the maintained private entry, naming what is missing when it is."""
    secret_id = SECRET_NAME.format(environment=environment)
    try:
        secret = secretsmanager.get_secret_value(SecretId=secret_id)
    except Exception as error:  # noqa: BLE001 - re-raised unless it is actionable setup guidance
        code = (getattr(error, "response", None) or {}).get("Error", {}).get("Code")
        if code in ("ResourceNotFoundException", "AccessDeniedException", "InvalidRequestException"):
            required = DOCUMENTED_KEYS + (("scope_editor", "scope_inputs", "credentials.editor") if scope_editor_enabled else ())
            raise ValueError(
                f"protected fixture configuration is unavailable ({code}) at {secret_id}: the fixture owner must "
                "publish a current version of the documented configuration holding " + ", ".join(required)
            ) from error
        raise
    body = secret.get("SecretString")
    if not isinstance(body, str) or not body:
        raise ValueError(f"protected fixture configuration at {secret_id} has no usable current version")
    if len(body) > MAX_CONFIG:
        raise ValueError("protected fixture configuration exceeds its bound")
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError("protected fixture configuration must be a JSON object")
    return document


def validate_configuration(config, *, base_url):
    """Accept only a complete, well-formed documented configuration."""
    missing = [key for key in DOCUMENTED_KEYS if key not in config]
    if missing:
        raise ValueError("protected fixture configuration is missing documented keys: " + ", ".join(missing))
    for key in TARGET_KEYS + IDENTITY_KEYS:
        if not isinstance(config[key], str) or not config[key]:
            raise ValueError("documented fixture configuration value is empty: " + key)
    if config["base_url"] != base_url:
        raise ValueError("protected fixture configuration targets another origin or run")
    if (
        config["tenant_id"] == config["nonmember_tenant_id"]
        or config["actor_id"] == config["nonmember_id"]
        or config["owner_subject"] == config["nonmember_subject"]
    ):
        raise ValueError("protected browser peers must be distinct ordinary identities")
    credentials = config["credentials"]
    if not isinstance(credentials, dict):
        raise ValueError("documented fixture credentials section must be an object")
    for peer in PEERS:
        peer_credentials = credentials.get(peer)
        if not isinstance(peer_credentials, dict) or not all(
            isinstance(peer_credentials.get(key), str) and peer_credentials.get(key) for key in CREDENTIAL_KEYS
        ):
            raise ValueError("documented fixture credentials are unusable for peer: " + peer)
    for key in ("plan_revision", "requirements_revision"):
        if not isinstance(config[key], str) or not REVISION.fullmatch(config[key]):
            raise ValueError("documented fixture revisions must be immutable commits: " + key)
    validate_backend_release(config["backend_release"])
    return config


def validate_backend_release(release):
    """The paired API release identity; a pairing is selected, never defaulted."""
    if not isinstance(release, dict) or set(release) != {"run_id", "attempt", "workflow_revision"}:
        raise ValueError("paired API release must name its run, attempt and workflow revision")
    for key, pattern in (("run_id", r"[1-9][0-9]{0,19}"), ("attempt", r"[1-9][0-9]{0,5}")):
        value = release[key]
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(pattern, str(value)):
            raise ValueError("paired API release identity is malformed: " + key)
    if not isinstance(release["workflow_revision"], str) or not REVISION.fullmatch(release["workflow_revision"]):
        raise ValueError("paired API release workflow revision must be an immutable commit")
    return release


def selected_backend_release(environment):
    """The API release pairing a dispatch selected for this run, if it selected one."""
    supplied = {
        key: str(environment.get("PRODUCT_BACKEND_RELEASE_" + key.upper(), "") or "")
        for key in ("run_id", "attempt", "workflow_revision")
    }
    if not any(supplied.values()):
        return None
    if not (
        re.fullmatch(r"[1-9][0-9]{0,19}", supplied["run_id"])
        and re.fullmatch(r"[1-9][0-9]{0,5}", supplied["attempt"])
        and REVISION.fullmatch(supplied["workflow_revision"])
    ):
        raise ValueError("selected API release pairing is incomplete or malformed")
    return validate_backend_release(supplied)


def check_release_pairing(config, environment):
    """Refuse a protected pairing that is not the one selected for this run."""
    maintained = validate_backend_release(config["backend_release"])
    selected = selected_backend_release(environment)
    if selected and {key: str(value) for key, value in selected.items()} != {
        key: str(value) for key, value in maintained.items()
    }:
        raise ValueError(
            "the protected API release pairing is not the pairing selected for this run: the maintained "
            "configuration must name the API release deployed for this source"
        )
    return maintained


def preflight(config, *, base_url, environment):
    """Prove the private configuration is usable before anything is claimed."""
    validate_configuration(config, base_url=base_url)
    return {
        "configuration": "complete",
        "tenant_id": config["tenant_id"],
        "nonmember_tenant_id": config["nonmember_tenant_id"],
        "backend_release": check_release_pairing(config, environment),
    }

def preflight_b03(config, *, base_url, environment):
    """Refuse incomplete scope setup before any run-owned state is claimed."""
    from tests.e2e.new_ui.product_scope_fixture import _inputs_valid

    missing = [key for key in DOCUMENTED_KEYS + ("scope_editor", "scope_inputs") if key not in config]
    editor = config.get("scope_editor")
    if isinstance(editor, dict):
        missing.extend("scope_editor." + key for key in ("actor_id", "subject", "team_id") if not editor.get(key))
    credentials = config.get("credentials")
    if isinstance(credentials, dict):
        editor_credentials = credentials.get("editor")
        if not isinstance(editor_credentials, dict):
            missing.append("credentials.editor")
        else:
            missing.extend("credentials.editor." + key for key in CREDENTIAL_KEYS if not editor_credentials.get(key))
    else:
        missing.append("credentials.editor")
    if missing:
        raise ValueError("B03 protected fixture configuration is missing: " + ", ".join(missing))
    proof = preflight(config, base_url=base_url, environment=environment)
    if not isinstance(editor, dict) or set(editor) != {"actor_id", "subject", "team_id"} or any(
        not isinstance(value, str) or not value for value in editor.values()
    ) or editor["actor_id"] in {config["actor_id"], config["nonmember_id"]} or editor["subject"] in {
        config["owner_subject"], config["nonmember_subject"]
    }:
        raise ValueError("B03 requires a distinct protected ordinary editor and team")
    for key in CREDENTIAL_KEYS:
        if not isinstance(credentials["editor"][key], str):
            raise ValueError("B03 fixture credentials are unusable for peer: editor")
    _inputs_valid(config["scope_inputs"])
    mode = config.get("scope_fixture_mode", "isolated")
    if mode not in ("isolated", "connected_scope"):
        raise ValueError("B03 requires an explicit supported scope fixture mode")
    if mode == "connected_scope" and not all(
        credentials[peer]["username"].startswith("eval-regression-") for peer in ("owner", "editor")
    ):
        raise ValueError("B03 connected scope requires dedicated ordinary fixture logins")
    return proof
