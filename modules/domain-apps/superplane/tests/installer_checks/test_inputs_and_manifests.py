import copy
import json
import subprocess
import sys

import pytest
import yaml

from installation.config import (
    COMPONENTS,
    LABEL,
    MODULE,
    Refusal,
    image,
    prepare_database_sql,
    validate,
)
from installation.manifests import bootstrap_job, migration_job, render

# Actual 44-char workspace cluster name produced by the workspace Terraform module.
# Must match the value in conftest.py's actual_name_environment fixture.
ACTUAL_WORKSPACE_CLUSTER = "adp-dev-spw-f67f322455acd6f6df9cb4bec015ffce"


def test_supported_input_and_four_service_runtime(environment, release):
    validate(environment, release)
    docs = render(environment, release)
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    assert set(deployments) == set(COMPONENTS)
    for name, deployment in deployments.items():
        pod = deployment["spec"]["template"]["spec"]
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        for language in ("java", "python", "nodejs", "dotnet"):
            assert (
                annotations[f"instrumentation.opentelemetry.io/inject-{language}"]
                == "false"
            )
        assert pod["containers"][0]["image"] == image(release, name)
        assert not pod.get("hostNetwork")
        assert pod["automountServiceAccountToken"] is False
        assert pod["containers"][0]["resources"]["limits"]
        assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    controller = deployments["superplane-controller"]["spec"]["template"]["spec"]
    env = {x["name"]: x.get("value") for x in controller["containers"][0]["env"]}
    assert env["SUPERPLANE_WORKSPACE_CREDENTIALS_DIR"] == "/workspace"
    assert "KUBECONFIG" not in env and "SKYPILOT_SERVICE_TOKEN" not in env
    assert controller["containers"][0]["args"] == ["--management-only"]
    assert not any(
        d["kind"]
        in {"ClusterRole", "ClusterRoleBinding", "Ingress", "PersistentVolume"}
        for d in docs
    )
    monitor = str(deployments["superplane-platform-monitor"])
    assert "DATABASE_URL" not in monitor
    assert "OBSERVATION_CREDENTIAL" in monitor


@pytest.mark.parametrize("management_only", [False, True])
def test_api_liveness_and_readiness_are_probed_separately(
    environment, release, management_only
):
    """Issue #5535: liveness is process health; readiness is control-plane health.

    Two endpoints in the app are worth nothing if the manifest points both probes at
    the same one. They must differ in both directions:

    * readiness on `/health` would report an API with an unreachable management
      database as ready, and Kubernetes would send it traffic it cannot serve;
    * liveness on `/readyz` would have Kubernetes kill and restart the pod whenever
      the database was briefly unreachable — turning a dependency blip into a crash
      loop for a fault no restart can fix, and removing the capacity that would
      serve requests once the dependency recovered.

    Parametrized over both modes because a control-plane-only installation is the
    case where the distinction matters most: it has no workspaces, so readiness must
    resolve from the management surface alone.
    """
    docs = render(environment, release, control_plane_only=management_only)
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    api = deployments["superplane-api"]["spec"]["template"]["spec"]["containers"][0]

    assert api["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert api["livenessProbe"]["httpGet"]["path"] == "/health"
    assert api["startupProbe"]["httpGet"]["path"] == "/health"


@pytest.mark.parametrize("management_only", [False, True])
def test_singleton_disruption_budgets_exclude_bootstrap_jobs(
    environment, release, management_only
):
    docs = render(environment, release, control_plane_only=management_only)
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    budgets = [d for d in docs if d["kind"] == "PodDisruptionBudget"]
    assert {d["metadata"]["name"] for d in budgets} == set(deployments)
    for budget in budgets:
        assert budget["apiVersion"] == "policy/v1"
        assert budget["spec"]["minAvailable"] == 1
        deployment = deployments[budget["metadata"]["name"]]
        selector = budget["spec"]["selector"]
        assert selector["matchLabels"] == {
            "app.kubernetes.io/name": deployment["metadata"]["name"],
            LABEL: deployment["metadata"]["labels"][LABEL],
        }
        assert (
            selector["matchLabels"].items()
            <= deployment["spec"]["template"]["metadata"]["labels"].items()
        )
        assert selector["matchExpressions"] == [
            {"key": "pod-template-hash", "operator": "Exists"}
        ]
    for job in (
        migration_job(environment, release, "test-run"),
        bootstrap_job(environment, release, "test-run"),
    ):
        assert "pod-template-hash" not in job["spec"]["template"]["metadata"]["labels"]


@pytest.mark.parametrize("management_only", [False, True])
def test_service_selectors_match_policy_peers(environment, release, management_only):
    """AWS must resolve owned Service IPs as well as pod IPs for egress."""
    docs = render(environment, release, control_plane_only=management_only)
    services = [doc for doc in docs if doc["kind"] == "Service"]
    assert len(services) == 4
    for service in services:
        selector = service["spec"]["selector"]
        assert selector[LABEL] == service["metadata"]["labels"][LABEL]
        deployment = next(
            doc
            for doc in docs
            if doc["kind"] == "Deployment"
            and doc["metadata"]["name"] == service["metadata"]["name"]
        )
        assert deployment["spec"]["selector"]["matchLabels"] == {
            "app.kubernetes.io/name": service["metadata"]["name"]
        }
        assert (
            deployment["spec"]["template"]["metadata"]["labels"][LABEL]
            == selector[LABEL]
        )
        matching_policies = [
            doc
            for doc in docs
            if doc["kind"] == "NetworkPolicy"
            and doc["metadata"]["name"] == service["metadata"]["name"]
        ]
        assert len(matching_policies) == 1
        peers = matching_policies[0]["spec"]["egress"][1]["to"]
        peer = next(
            peer
            for peer in peers
            if peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            == service["metadata"]["namespace"]
        )
        assert peer["podSelector"]["matchLabels"].items() <= selector.items()


def test_skypilot_database_config_does_not_override_secret(environment, release):
    docs = render(environment, release)
    configmap = next(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "skypilot-config"
    )
    assert "db" not in yaml.safe_load(configmap["data"]["config.yaml"])
    assert yaml.safe_load(configmap["data"]["config.yaml"]) == {}
    desired = yaml.safe_load(configmap["data"]["desired-config.yaml"])
    assert "db" not in desired
    assert desired["allowed_clouds"] == release["skypilot_config"]["allowed_clouds"]
    assert desired["kubernetes"]["remote_identity"] == "SERVICE_ACCOUNT"
    deployment = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "skypilot-api"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "IS_SKYPILOT_SERVER", "value": "true"} in container["env"]
    assert container["command"] == ["python3", "/skypilot-bootstrap/bootstrap.py"]
    database = next(
        v for v in container["env"] if v["name"] == "SKYPILOT_DB_CONNECTION_URI"
    )
    assert database["valueFrom"]["secretKeyRef"] == {
        "name": "skypilot-api-db",
        "key": "connection-uri",
        "optional": False,
    }


def test_skypilot_username_resolves_without_a_passwd_entry(environment, release):
    deployment = next(
        doc
        for doc in render(environment, release)
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "skypilot-api"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    values = {
        item["name"]: item["value"] for item in container["env"] if "value" in item
    }
    # Reproduce the pinned image's missing passwd entry and lack of an ambient
    # login name. HOME alone does not satisfy SkyPilot's getpass.getuser call.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import getpass,pwd; from unittest.mock import patch; "
            "p=patch.object(pwd,'getpwuid',side_effect=KeyError('uid not found')); "
            "p.start(); print(getpass.getuser())",
        ],
        env=values,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "skypilot"


def test_actual_44_char_workspace_cluster_name_is_accepted(
    actual_name_environment, release
):
    """The 44-char Terraform-generated workspace cluster name must pass validation."""
    validate(actual_name_environment, release)
    docs = render(actual_name_environment, release)
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    controller = deployments["superplane-controller"]["spec"]["template"]["spec"]
    env_vars = {x["name"]: x.get("value") for x in controller["containers"][0]["env"]}
    # The durable registry supplies target identity; no ambient cluster selector.
    assert env_vars["SUPERPLANE_WORKSPACE_CREDENTIALS_DIR"] == "/workspace"
    assert "EKS_CLUSTER_NAME" not in env_vars


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Oversized: 101-char cluster name exceeds EKS limit
        ("workspace_cluster", "a" * 101),
        # Invalid character in cluster name
        ("workspace_cluster", "cluster.with.dots"),
        ("workspace_cluster", "cluster with spaces"),
        # Non-alphanumeric first character
        ("workspace_cluster", "-starts-with-hyphen"),
        ("workspace_cluster", "_starts-with-underscore"),
    ],
)
def test_reject_invalid_eks_cluster_names(environment, release, key, value):
    environment[key] = value
    with pytest.raises(Refusal, match="Invalid"):
        validate(environment, release)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("namespace", "adp"),
        ("namespace", "kube-system"),
        ("namespace", "x;touch /tmp/pwn"),
        ("origin", "http://adp.example.test"),
        ("origin", "https://user:password@adp.example.test"),
        ("account_id", "wrong"),
        ("network_policy_enforced", False),
        ("controller_ownership", "unknown"),
        ("workspace_cluster", "adp-dev-eks-cluster"),
        ("timeout_seconds", True),
    ],
)
def test_reject_unsafe_environment(environment, release, key, value):
    environment[key] = value
    with pytest.raises(Refusal):
        validate(environment, release)


@pytest.mark.parametrize(
    "schema",
    [
        "public",
        "pg_catalog",
        "superplane,public",
        "x;DROP TABLE users",
        "information_schema",
    ],
)
def test_schema_boundary(environment, release, schema):
    environment["database"]["schema"] = schema
    with pytest.raises(Refusal):
        validate(environment, release)


def test_reject_stale_image_provenance_and_schema(environment, release):
    stale = copy.deepcopy(release)
    stale["image_sources"]["superplane-api"]["source_revision"] = "unresolved"
    with pytest.raises(Refusal, match="source"):
        validate(environment, stale)
    stale = copy.deepcopy(release)
    stale["schema"]["observed"]["head"] = "013_add_provider_operations"
    with pytest.raises(Refusal, match="release schema"):
        validate(environment, stale)


def test_migration_runs_maintained_image_and_actual_settings(environment, release):
    job = migration_job(environment, release, "test123")
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "app.installation", "migrate"]
    variables = {e["name"]: e for e in container["env"]}
    assert (
        variables["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "migration-url"
    )
    assert variables["SUPERPLANE_DB_SCHEMA"]["value"] == "superplane"
    assert job["spec"]["backoffLimit"] == 0
    assert "PGOPTIONS" not in variables


def test_cli_plan_never_invokes_cloud_or_platform(tmp_path, environment, release):
    config, lock = tmp_path / "environment.yaml", tmp_path / "lock.yaml"
    config.write_text(yaml.safe_dump(environment))
    lock.write_text(yaml.safe_dump(release))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "installation",
            "--environment",
            str(config),
            "--release-lock",
            str(lock),
            "--output",
            str(tmp_path / "plan"),
        ],
        cwd=MODULE,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "plan/receipt.json").read_text())
    assert receipt["status"] == "planned"
    assert receipt["completed"] == []
    assert "installed-and-verified" not in result.stdout
    assert (
        len(list(yaml.safe_load_all((tmp_path / "plan/manifests.yaml").read_text())))
        > 15
    )


def test_unknown_secret_reference_is_rejected(environment, release):
    environment["secrets"]["unexpected"] = "adp/dev/another-domain/database"
    with pytest.raises(Refusal, match="exactly the documented domain-owned"):
        validate(environment, release)


def test_runtime_role_annotations_match_maintained_terraform(environment, release):
    import re

    source = (MODULE / "infra/control-plane/irsa.tf").read_text()
    for doc in render(environment, release):
        if (
            doc["kind"] != "ServiceAccount"
            or doc["metadata"]["name"] == "superplane-platform-monitor"
        ):
            continue
        role = (
            "skypilot" if doc["metadata"]["name"] == "skypilot-api" else "control_plane"
        )
        name = re.search(
            r'resource "aws_iam_role" "' + role + r'"\s*\{.*?name\s*=\s*"([^"]+)"',
            source,
            re.S,
        ).group(1)
        name = name.replace(
            "${local.name_prefix}", "adp-" + environment["environment"] + "-superplane"
        )
        assert (
            doc["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
            == f"arn:aws:iam::{environment['account_id']}:role/{name}"
        )


# --- Control-plane-only mode ---


def _cp_only_environment(environment):
    """Strip workspace fields to produce a valid control-plane-only environment."""
    env = copy.deepcopy(environment)
    env["control_plane_only"] = True
    for key in (
        "workspace_cluster",
        "workspace_namespace",
        "workspace_id",
        "cluster_id",
    ):
        env.pop(key, None)
    env.pop("controller_ownership", None)
    env.pop("execution", None)
    env.pop("controller_profiles", None)
    # Remove workspace_access from secrets; only database and observation required.
    env["secrets"] = {
        k: v for k, v in env["secrets"].items() if k != "workspace_access"
    }
    return env


def test_control_plane_only_validate_accepts_missing_workspace_fields(
    environment, release
):
    """validate() with control_plane_only=True must accept an env without workspace fields."""
    env = _cp_only_environment(environment)
    # Must not raise — control-plane-only is a valid first-deployment configuration.
    validate(env, release, control_plane_only=True)


def test_full_install_still_requires_workspace_fields(environment, release):
    """Without control_plane_only, workspace fields remain mandatory."""
    env = _cp_only_environment(environment)
    env.pop("control_plane_only", None)
    with pytest.raises(Refusal):
        validate(env, release, control_plane_only=False)


def test_control_plane_only_renders_management_controller(environment, release):
    """The management controller has registry authority and no workspace fallback."""
    env = _cp_only_environment(environment)
    docs = render(env, release, control_plane_only=True)
    names = [d["metadata"].get("name") for d in docs]
    assert "superplane-controller" in names
    controller = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment"
        and doc["metadata"]["name"] == "superplane-controller"
    )
    pod = controller["spec"]["template"]["spec"]
    assert pod["containers"][0]["args"] == ["--management-only"]
    assert pod["automountServiceAccountToken"] is False
    assert all(
        volume["secret"]["optional"]
        for volume in pod["volumes"]
        if "workspace" in volume["name"]
    )
    # API and monitor must still be present.
    assert "superplane-api" in names
    assert "superplane-platform-monitor" in names


def test_control_plane_only_render_has_no_workspace_references(environment, release):
    """Manifests rendered in control-plane-only mode must not reference workspace fields."""
    env = _cp_only_environment(environment)
    docs = render(env, release, control_plane_only=True)
    serialized = yaml.safe_dump_all(docs)
    # Optional named projections permit later registration without making
    # credentials a prerequisite for healthy zero-target startup.
    assert "optional: true" in serialized
    assert environment["workspace_id"] not in serialized
    assert environment["cluster_id"] not in serialized


def test_cli_mode_reaches_exact_bootstrap_payload(tmp_path, environment, release):
    from installation.runner import Installer

    env = _cp_only_environment(environment)
    env.pop("control_plane_only")
    installer = Installer(env, release, tmp_path, control_plane_only=True)
    job = bootstrap_job(installer.env, release, "example")
    fields = job["spec"]["template"]["spec"]["containers"][0]["env"]
    config = json.loads(
        next(
            value["value"]
            for value in fields
            if value["name"] == "SUPERPLANE_BOOTSTRAP_CONFIG"
        )
    )
    assert config == {
        "control_plane_only": True,
        **{key: env[key] for key in ("adp_org_id", "org_id", "origin")},
    }


@pytest.mark.parametrize("value", ["false", "true", 1, None])
def test_mode_is_a_boolean_not_python_truthiness(environment, release, value):
    environment["control_plane_only"] = value
    with pytest.raises(Refusal, match="boolean"):
        validate(environment, release)


def test_preparation_accepts_full_environment_without_workspace_readiness(environment):
    environment["network_policy_enforced"] = False
    environment["controller_ownership"] = None
    validate(environment, None, preparation=True)


# --- Portable prepare-database SQL ---


def test_prepare_database_sql_derives_names_from_environment(environment):
    """SQL output must use schema/role names from the environment, not hardcoded values."""
    sql = prepare_database_sql(environment)
    db = environment["database"]
    env_name = environment["environment"]
    # Schema names are from the environment config.
    assert f'CREATE SCHEMA "{db["schema"]}" AUTHORIZATION' in sql
    assert f'CREATE SCHEMA "{db["skypilot_schema"]}" AUTHORIZATION' in sql
    # Role names are derived from environment name and must not contain account IDs.
    assert f"superplane_{env_name}_runtime" in sql
    assert f"superplane_{env_name}_migration" in sql
    assert f"superplane_{env_name}_skypilot" in sql
    assert environment["account_id"] not in sql


def test_prepare_database_sql_different_environment_produces_different_roles():
    """Two different environments must produce non-overlapping role names."""
    env_a = {
        "environment": "dev",
        "database": {
            "schema": "superplane",
            "skypilot_schema": "skypilot",
            "database": "bedrockgateway",
        },
    }
    env_b = {
        "environment": "staging",
        "database": {
            "schema": "superplane",
            "skypilot_schema": "skypilot",
            "database": "bedrockgateway",
        },
    }
    sql_a = prepare_database_sql(env_a)
    sql_b = prepare_database_sql(env_b)
    # Role names include the environment name — they must differ between environments.
    assert "superplane_dev_runtime" in sql_a
    assert "superplane_staging_runtime" in sql_b
    assert "superplane_staging_runtime" not in sql_a
    assert "superplane_dev_runtime" not in sql_b


def test_prepare_database_sql_contains_no_credentials(environment):
    """SQL output must contain only schema/role DDL and no secret values."""
    sql = prepare_database_sql(environment)
    # No URL patterns, no password-like values.
    assert "postgresql://" not in sql
    assert "password" not in sql.lower()
    assert "secret" not in sql.lower()


def test_prepare_database_cli_writes_sql_and_returns_zero(
    tmp_path, environment, release
):
    """--prepare-database must write a .sql file and exit 0 without contacting AWS."""
    config = tmp_path / "environment.yaml"
    env = _cp_only_environment(environment)
    config.write_text(yaml.safe_dump(env))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "installation",
            "--environment",
            str(config),
            "--output",
            str(tmp_path / "prepare"),
            "--prepare-database",
        ],
        cwd=MODULE,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "prepared"
    sql_path = tmp_path / "prepare" / "prepare-database.sql"
    assert sql_path.exists()
    sql = sql_path.read_text()
    assert 'CREATE SCHEMA "superplane" AUTHORIZATION' in sql
    # No AWS calls should be in the output.
    assert "sts" not in result.stdout
    assert "aws" not in result.stderr


def test_installer_pins_production_auth_and_reviewed_origin(environment, release):
    docs = render(environment, release)
    api = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "superplane-api"
    )
    variables = {
        item["name"]: item
        for item in api["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert variables["SUPERPLANE_SECURITY_PROFILE"]["value"] == "production"
    assert variables["DOMAIN_AUTH_ENFORCED"]["value"] == "true"
    assert variables["COGNITO_ISSUER"]["value"] == environment["auth"]["issuer"]
    assert (
        json.loads(variables["DOMAIN_AUTH_ALLOWED_CLIENT_IDS"]["value"])
        == environment["auth"]["client_ids"]
    )
    assert json.loads(variables["CORS_ORIGINS"]["value"]) == [environment["origin"]]
    assert "secretKeyRef" in variables["JWT_SECRET_KEY"]["valueFrom"]
