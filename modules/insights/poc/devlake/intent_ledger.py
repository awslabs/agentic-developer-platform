import os
import re


IMAGE = re.compile(r"[^\s]+@sha256:[a-f0-9]{64}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
ACCOUNT = re.compile(r"[0-9]{12}\Z")
REGION = re.compile(r"[a-z]{2}-[a-z]+-[0-9]\Z")
CELL = re.compile(r"[a-z][a-z0-9-]{0,30}\Z")
RESOURCE = re.compile(r"(?:db|cluster)-[A-Za-z0-9]+\Z")
DATABASE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")


def checked(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("exact optional resource identity is required")
    return value


def collection(config, variables):
    account = checked(config["aws_account_id"], ACCOUNT)
    region = checked(config["aws_region"], REGION)
    environment = config["environment"]
    if environment not in ("dev", "prod"):
        raise ValueError("exact optional environment is required")
    cell = checked(config["cell_id"], CELL)
    resource = checked(config["existing_db_resource_id"], RESOURCE)
    database = checked(config["database_name"], DATABASE)
    image = checked(variables["worker_image"], IMAGE)
    schema = checked(variables["schema_manifest_sha256"], DIGEST)
    if any(variables[key] != config[key] for key in
           ("aws_account_id", "aws_region", "environment", "cell_id", "existing_db_resource_id")):
        raise ValueError("collection plan differs from registered cell")

    namespace = "adp-insights-devlake-" + cell
    name = f"adp-{environment}-insights-devlake-{cell}-worker"
    worker = "devlake-" + cell + "-worker"
    return {
        "module": "collection", "environment": environment, "cell_id": cell,
        "account": account, "region": region,
        "state_key": f"{environment}/modules/insights/devlake/cells/{cell}/terraform.tfstate",
        "owned": {
            "iam_role": name,
            "iam_role_policy": name + "/cell-db-connect",
            "kubernetes_namespace": namespace,
            "kubernetes_service_account": namespace + "/" + worker,
            "kubernetes_network_policy": namespace + "/deny-all",
            "kubernetes_deployment": namespace + "/" + worker,
        },
        "retained": {"db_resource_id": resource, "database": database,
                     "ca_configmap": checked(config["ca_configmap_name"], DATABASE)},
        "image": image, "schema_sha256": schema, "state": "registered",
    }


def connectors(config, image):
    account = checked(config["aws_account_id"], ACCOUNT)
    region = checked(config["aws_region"], REGION)
    environment = config["environment"]
    if environment not in ("dev", "prod"):
        raise ValueError("exact optional environment is required")
    name = f"adp-{environment}-devlake-connectors-api"
    namespace = "adp-devlake-connectors"
    return {
        "module": "connectors", "environment": environment, "account": account, "region": region,
        "state_key": f"{environment}/modules/connectors/devlake/terraform.tfstate",
        "owned": {
            "iam_role": name,
            "kubernetes_namespace": namespace,
            "kubernetes_service_account": namespace + "/connector-api",
            "kubernetes_network_policy_deny": namespace + "/deny-all",
            "kubernetes_network_policy_allow": namespace + "/gateway-broker-and-provider",
            "kubernetes_deployment": namespace + "/connector-api",
            "kubernetes_service": namespace + "/connector-api",
        },
        "retained": {}, "image": checked(image, IMAGE), "state": "registered",
    }


def validate(intents, account, region, artifacts):
    if not isinstance(intents, dict):
        raise ValueError("protected optional intent ledger differs")
    states = set()
    owned = set()
    for key, intent in intents.items():
        if not isinstance(intent, dict) or intent.get("account") != account or intent.get("region") != region:
            raise ValueError("protected optional intent target differs")
        if not isinstance(intent.get("retained"), dict):
            raise ValueError("protected optional retention references differ")
        config = {"aws_account_id": account, "aws_region": region,
                  "environment": intent.get("environment"), "cell_id": intent.get("cell_id"),
                  "existing_db_resource_id": intent.get("retained", {}).get("db_resource_id"),
                  "database_name": intent.get("retained", {}).get("database"),
                  "ca_configmap_name": intent.get("retained", {}).get("ca_configmap")}
        if key == "connectors" and intent.get("module") == "connectors":
            expected = connectors(config, intent.get("image"))
        elif key == "collection:" + str(intent.get("cell_id")) and intent.get("module") == "collection":
            variables = {"aws_account_id": account, "aws_region": region,
                         "environment": intent.get("environment"), "cell_id": intent.get("cell_id"),
                         "existing_db_resource_id": config["existing_db_resource_id"],
                         "worker_image": intent.get("image"),
                         "schema_manifest_sha256": intent.get("schema_sha256")}
            expected = collection(config, variables)
        else:
            raise ValueError("protected optional intent kind differs")
        if expected != intent:
            raise ValueError("protected optional intent has changed")
        artifact = artifacts.get("api" if key == "connectors" else "worker", {})
        if intent["image"] != artifact.get("repository_uri", "") + "@" + artifact.get("digest", ""):
            raise ValueError("optional intent image differs from protected artifact")
        if intent["state_key"] in states or any(item in owned for item in intent["owned"].items()):
            raise ValueError("optional intents share state or owned resources")
        states.add(intent["state_key"])
        owned.update(intent["owned"].items())


def register(finalize, key, intent):
    label = os.environ.get("ADP_DEVLAKE_INTENT_CONNECTION_LABEL", "")
    if not label:
        raise ValueError("protected optional intent requires a finalizer connection")

    def read():
        return finalize.get_registered("finalizer", label_override=label)

    scope, previous, record = read()
    if (intent["account"] != scope[2] or intent["region"] != scope[3] or
            record.get("finalization_requested") or record.get("finalization_complete")):
        raise ValueError("optional intent authority or terminal state differs")
    validate({key: intent}, scope[2], scope[3], record["artifacts"])
    existing = record.get("optional_resources", {}).get(key)
    if existing is not None:
        if existing != intent:
            raise ValueError("optional resource intent cannot be replaced")
        return intent
    validate({**record.get("optional_resources", {}), key: intent},
             scope[2], scope[3], record["artifacts"])
    record.setdefault("optional_resources", {})[key] = intent
    try:
        finalize.save(scope, previous, record)
    except (OSError, ValueError):
        scope, _, current = read()
        if (current.get("finalization_requested") or current.get("finalization_complete") or
                current.get("optional_resources", {}).get(key) != intent):
            raise ValueError("protected optional intent update was not confirmed") from None
    _, _, current = read()
    if (current.get("finalization_requested") or current.get("finalization_complete") or
            current.get("optional_resources", {}).get(key) != intent):
        raise ValueError("protected optional intent readback differs")
    return intent


def require_cleanup_authority():
    raise ValueError("optional_cleanup_required: qualified optional-resource finalizer is unavailable")
