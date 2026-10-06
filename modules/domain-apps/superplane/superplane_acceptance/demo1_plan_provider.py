"""Read-only inventory of the maintained infra/workspaces dedicated-cluster plan.

Names are the exact relationships in main.tf, iam.tf, eks.tf and node_network.tf;
provider-assigned child identities come from the original cluster and nodegroup.
This module does not infer delete authority or attempt any provider mutation.
"""

import json
import re

from .demo1_evidence import EvidenceError

PLAN_VERSION = "infra/workspaces-dedicated-retained-kms-v1"
MAX_ITEMS = 200
MAX_PAGES = 32
ROLE_SUFFIXES = ("-cluster-role", "-node-role", "-vpc-cni-role", "-admin")


def require(condition):
    if not condition:
        raise EvidenceError("workspace plan provider evidence incomplete or mismatched")


def parse_resource(arn, query):
    require(isinstance(arn, str) and len(arn) <= 1024)
    parts = arn.split(":", 5)
    require(
        len(parts) == 6
        and parts[0] == "arn"
        and parts[1] == query.expected_owned[0].split(":")[1]
        and parts[4] == query.account
    )
    service, region, value = parts[2], parts[3], parts[5]
    require(region == ("" if service == "iam" else query.region))
    expressions = {
        "iam": (
            ("role", r"role/([A-Za-z0-9+=,.@_-]{1,64})"),
            ("instance-profile", r"instance-profile/([A-Za-z0-9+=,.@_/-]{1,512})"),
            (
                "oidc-provider",
                rf"oidc-provider/(oidc\.eks\.{re.escape(query.region)}\.amazonaws\.com(?:\.cn)?/id/[A-Za-z0-9]+)",
            ),
        ),
        "logs": (("log-group", r"log-group:(/aws/eks/[A-Za-z0-9_-]+/cluster)"),),
        "eks": (
            ("nodegroup", r"nodegroup/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9-]+)"),
            ("addon", r"addon/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9-]+)"),
        ),
        "autoscaling": (
            (
                "autoscaling-group",
                r"autoScalingGroup:([A-Za-z0-9-]+:autoScalingGroupName/[A-Za-z0-9_.:/+=@-]{1,255})",
            ),
        ),
        "ec2": (
            ("launch-template", r"launch-template/(lt-[0-9a-f]{8}(?:[0-9a-f]{9})?)"),
        ),
        "kms": (("key", r"key/([A-Za-z0-9-]{1,128})"),),
    }
    for kind, pattern in expressions.get(service, ()):
        matched = re.fullmatch(pattern, value)
        if matched:
            return kind, matched[1]
    raise EvidenceError("workspace plan provider resource unsupported")


def call(reader, query, service, operation, arguments, *, missing=None):
    require(reader._identity())
    code, value, message = reader._execute(
        service, operation, *arguments, "--region", query.region, "--no-paginate"
    )
    if code:
        api = (
            "GetOpenIDConnectProvider"
            if operation == "get-open-id-connect-provider"
            else "".join(part.title() for part in operation.split("-"))
        )
        # Each supported missing response is from a single exact resource request.
        # Some EKS/IAM missing responses omit the supplied ID in their text.
        require(
            code in (254, 255)
            and missing
            and re.fullmatch(
                rf"\s*An error occurred \({re.escape(missing)}\) when calling the {api} operation: [^\r\n]+\s*",
                message,
            )
        )
        return None
    require(isinstance(value, dict))
    return value


def pages(reader, query, service, operation, arguments, collection, *, iam=False):
    token, seen, result, rows_seen = None, set(), [], set()
    for _ in range(MAX_PAGES):
        value = call(
            reader,
            query,
            service,
            operation,
            [
                *arguments,
                *(("--marker" if iam else "--next-token", token) if token else ()),
            ],
        )
        require(isinstance(value.get(collection), list))
        for row in value[collection]:
            encoded = json.dumps(row, sort_keys=True)
            require(encoded not in rows_seen)
            rows_seen.add(encoded)
        result.extend(value[collection])
        require(len(result) <= MAX_ITEMS)
        if iam:
            require(type(value.get("IsTruncated")) is bool)
            require(
                not value.get("NextToken")
                and not value.get("nextToken")
                and not value.get("PaginationToken")
            )
            require(value["IsTruncated"] or not value.get("Marker"))
            token = value.get("Marker") if value["IsTruncated"] else None
        else:
            require(not value.get("Marker") and not value.get("IsTruncated"))
            token_key = "NextToken" if service == "autoscaling" else "nextToken"
            require(
                not any(
                    value.get(key)
                    for key in ("NextToken", "nextToken", "PaginationToken")
                    if key != token_key
                )
            )
            token = value.get(token_key)
        if token in (None, ""):
            require(not iam or value["IsTruncated"] is False)
            return result
        require(isinstance(token, str) and len(token) <= 4096 and token not in seen)
        seen.add(token)
    raise EvidenceError("workspace plan provider pagination exceeded")


def object_for(reader, query, arn):
    kind, name = parse_resource(arn, query)
    if kind == "log-group":
        rows = pages(
            reader,
            query,
            "logs",
            "describe-log-groups",
            ["--log-group-name-prefix", name],
            "logGroups",
        )
        require(
            all(
                isinstance(row, dict)
                and isinstance(row.get("logGroupName"), str)
                and row["logGroupName"].startswith(name)
                for row in rows
            )
        )
        rows = [row for row in rows if row["logGroupName"] == name]
        require(len(rows) <= 1)
        if not rows:
            return None
        row = rows[0]
        observed_arn = row.get("logGroupArn", row.get("arn"))
        require(
            isinstance(observed_arn, str) and observed_arn.removesuffix(":*") == arn
        )
        return row
    if kind == "autoscaling-group":
        group = name.split(":autoScalingGroupName/", 1)[1]
        rows = pages(
            reader,
            query,
            "autoscaling",
            "describe-auto-scaling-groups",
            ["--auto-scaling-group-names", group],
            "AutoScalingGroups",
        )
        require(len(rows) <= 1)
        if not rows:
            return None
        row = rows[0]
        require(
            isinstance(row, dict)
            and row.get("AutoScalingGroupARN") == arn
            and row.get("AutoScalingGroupName") == group
        )
        return row
    descriptors = {
        "role": (
            "iam",
            "get-role",
            ["--role-name", name],
            "Role",
            "Arn",
            "NoSuchEntity",
        ),
        "instance-profile": (
            "iam",
            "get-instance-profile",
            ["--instance-profile-name", name.rsplit("/", 1)[-1]],
            "InstanceProfile",
            "Arn",
            "NoSuchEntity",
        ),
        "oidc-provider": (
            "iam",
            "get-open-id-connect-provider",
            ["--open-id-connect-provider-arn", arn],
            None,
            None,
            "NoSuchEntity",
        ),
        "key": (
            "kms",
            "describe-key",
            ["--key-id", arn],
            "KeyMetadata",
            "Arn",
            "NotFoundException",
        ),
    }
    if kind in {"nodegroup", "addon"}:
        cluster, child, _ = name.split("/")
        descriptor = (
            "eks",
            "describe-" + kind,
            [
                "--cluster-name",
                cluster,
                "--" + ("nodegroup-name" if kind == "nodegroup" else "addon-name"),
                child,
            ],
            kind,
            "nodegroupArn" if kind == "nodegroup" else "addonArn",
            "ResourceNotFoundException",
        )
    else:
        descriptor = descriptors[kind]
    service, operation, arguments, field, identity, missing = descriptor
    value = call(reader, query, service, operation, arguments, missing=missing)
    if value is None:
        return None
    require(
        not any(
            value.get(key)
            for key in (
                "Marker",
                "NextToken",
                "nextToken",
                "IsTruncated",
                "PaginationToken",
            )
        )
    )
    row = value.get(field) if field else value
    require(isinstance(row, dict))
    if identity:
        require(row.get(identity) == arn)
    if kind == "oidc-provider":
        require(row.get("Url", "").removeprefix("https://") == name)
    if kind == "key":
        require(
            row.get("AWSAccountId") == query.account
            and isinstance(row.get("Enabled"), bool)
            and row.get("KeyState")
            in {
                "Enabled",
                "Disabled",
                "PendingDeletion",
                "PendingImport",
                "Unavailable",
                "Creating",
                "Updating",
                "PendingReplicaDeletion",
            }
        )
    return row


def exact_state(reader, query, arn):
    row = object_for(reader, query, arn)
    if row is None:
        return "absent"
    kind, _ = parse_resource(arn, query)
    if kind == "key" and (row["Enabled"] is not True or row["KeyState"] != "Enabled"):
        return "unavailable"
    return "present"


def tagged(row, selected, query):
    tags = row.get("Tags", row.get("tags"))
    if isinstance(tags, list):
        require(
            all(
                isinstance(item, dict)
                and isinstance(item.get("Key"), str)
                and isinstance(item.get("Value"), str)
                for item in tags
            )
        )
        require(len(tags) == len({item["Key"] for item in tags}))
        tags = {item["Key"]: item["Value"] for item in tags}
    require(
        isinstance(tags, dict)
        and tags.get("OrgId") == selected.org_id
        and tags.get("WorkspaceId") == query.workspace_id
    )


def capture_plan(reader, query, selected):
    """Capture the concrete maintained plan and linked provider child identities."""
    cluster_arn = next(arn for arn in query.expected_owned if ":cluster/" in arn)
    cluster = cluster_arn.rsplit("/", 1)[1]
    partition = cluster_arn.split(":")[1]
    regional = f"arn:{partition}"
    role_prefix = f"{regional}:iam::{query.account}:role/{cluster}"
    value = call(reader, query, "eks", "describe-cluster", ["--name", cluster])
    row = value.get("cluster")
    require(
        isinstance(row, dict)
        and row.get("arn") == cluster_arn
        and row.get("roleArn") == role_prefix + "-cluster-role"
    )
    tagged(row, selected, query)
    keys = row.get("encryptionConfig")
    require(
        isinstance(keys, list)
        and len(keys) == 1
        and isinstance(keys[0], dict)
        and isinstance(keys[0].get("provider"), dict)
    )
    key = keys[0]["provider"].get("keyArn")
    require(parse_resource(key, query)[0] == "key" and key in query.expected_survivors)
    identity = row.get("identity")
    require(isinstance(identity, dict) and isinstance(identity.get("oidc"), dict))
    issuer = identity["oidc"].get("issuer")
    require(isinstance(issuer, str) and issuer.startswith("https://"))
    oidc = f"{regional}:iam::{query.account}:oidc-provider/{issuer.removeprefix('https://')}"
    require(parse_resource(oidc, query)[0] == "oidc-provider")
    log = f"{regional}:logs:{query.region}:{query.account}:log-group:/aws/eks/{cluster}/cluster"
    resources, optional = {}, []
    for suffix in ROLE_SUFFIXES:
        arn = role_prefix + suffix
        role = object_for(reader, query, arn)
        if role is None:
            require(suffix == "-admin")
            resources[arn] = "absent"
        else:
            tagged(role, selected, query)
            resources[arn] = "present"
        if suffix == "-admin":
            optional.append(arn)
    require(object_for(reader, query, oidc) is not None)
    resources[oidc] = "present"
    logs = object_for(reader, query, log)
    require(logs is not None and logs.get("kmsKeyId") == key)
    resources[log] = "present"
    require(exact_state(reader, query, key) == "present")
    for kind, required in (("nodegroup", cluster + "-default"), ("addon", "vpc-cni")):
        collection = "nodegroups" if kind == "nodegroup" else "addons"
        names = pages(
            reader,
            query,
            "eks",
            "list-" + collection,
            ["--cluster-name", cluster],
            collection,
        )
        require(
            all(
                isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name)
                for name in names
            )
            and len(names) == len(set(names))
            and required in names
        )
        for name in names:
            value = call(
                reader,
                query,
                "eks",
                "describe-" + kind,
                [
                    "--cluster-name",
                    cluster,
                    "--" + ("nodegroup-name" if kind == "nodegroup" else "addon-name"),
                    name,
                ],
            )
            child = value.get(kind)
            field = "nodegroupArn" if kind == "nodegroup" else "addonArn"
            require(isinstance(child, dict))
            arn = child.get(field)
            parsed_kind, identity = parse_resource(arn, query)
            require(parsed_kind == kind and identity.split("/")[:2] == [cluster, name])
            resources[arn] = "present"
            if kind == "nodegroup":
                require(child.get("nodeRole") == role_prefix + "-node-role")
                template = child.get("launchTemplate")
                require(
                    isinstance(template, dict)
                    and isinstance(template.get("id"), str)
                    and re.fullmatch(r"lt-[0-9a-f]{8}(?:[0-9a-f]{9})?", template["id"])
                )
                template_arn = f"arn:{partition}:ec2:{query.region}:{query.account}:launch-template/{template['id']}"
                resources[template_arn] = "present"
                linked = child.get("resources")
                require(isinstance(linked, dict))
                groups = linked.get("autoScalingGroups")
                require(isinstance(groups, list) and 0 < len(groups) <= MAX_ITEMS)
                for group in groups:
                    require(
                        isinstance(group, dict) and isinstance(group.get("name"), str)
                    )
                    groups = pages(
                        reader,
                        query,
                        "autoscaling",
                        "describe-auto-scaling-groups",
                        ["--auto-scaling-group-names", group["name"]],
                        "AutoScalingGroups",
                    )
                    require(
                        len(groups) == 1
                        and isinstance(groups[0], dict)
                        and groups[0].get("AutoScalingGroupName") == group["name"]
                    )
                    group_arn = groups[0].get("AutoScalingGroupARN")
                    require(parse_resource(group_arn, query)[0] == "autoscaling-group")
                    resources[group_arn] = "present"
            elif name == "vpc-cni":
                require(
                    child.get("serviceAccountRoleArn") == role_prefix + "-vpc-cni-role"
                )
    profiles = pages(
        reader,
        query,
        "iam",
        "list-instance-profiles-for-role",
        ["--role-name", cluster + "-node-role"],
        "InstanceProfiles",
        iam=True,
    )
    for profile in profiles:
        require(
            isinstance(profile, dict)
            and parse_resource(profile.get("Arn"), query)[0] == "instance-profile"
        )
        roles = profile.get("Roles")
        require(
            isinstance(roles, list)
            and len(roles) == 1
            and isinstance(roles[0], dict)
            and roles[0].get("Arn") == role_prefix + "-node-role"
        )
        resources[profile["Arn"]] = "present"
    result = {
        "version": PLAN_VERSION,
        "cluster_arn": cluster_arn,
        "retained_key": key,
        "resources": resources,
        "optional": optional,
    }
    validate_plan(result, query)
    return result


def validate_plan(value, query):
    require(
        isinstance(value, dict)
        and set(value)
        == {"version", "cluster_arn", "retained_key", "resources", "optional"}
        and value["version"] == PLAN_VERSION
        and value["cluster_arn"] in query.expected_owned
        and ":cluster/" in value["cluster_arn"]
        and value["retained_key"] in query.expected_survivors
    )
    require(parse_resource(value["retained_key"], query)[0] == "key")
    cluster = value["cluster_arn"].rsplit("/", 1)[1]
    partition = value["cluster_arn"].split(":")[1]
    prefix = f"arn:{partition}:iam::{query.account}:role/{cluster}"
    roles = {prefix + suffix for suffix in ROLE_SUFFIXES}
    require(
        isinstance(value["resources"], dict)
        and len(value["resources"]) <= MAX_ITEMS
        and roles <= value["resources"].keys()
        and value["optional"] == [prefix + "-admin"]
    )
    kinds = set()
    for arn, state in value["resources"].items():
        kind, name = parse_resource(arn, query)
        kinds.add(kind)
        require(state == "present" or (arn in value["optional"] and state == "absent"))
        require(kind != "key" and arn not in query.expected_survivors)
        if kind == "role":
            require(arn in roles)
        if kind in {"nodegroup", "addon"}:
            require(name.split("/")[0] == cluster)
        if kind == "log-group":
            require(name == f"/aws/eks/{cluster}/cluster")
    require(
        {
            "role",
            "oidc-provider",
            "log-group",
            "nodegroup",
            "addon",
            "autoscaling-group",
            "launch-template",
        }
        <= kinds
    )
    require(
        any(
            ":nodegroup/" + cluster + "/" + cluster + "-default/" in arn
            for arn in value["resources"]
        )
    )
    require(any(":addon/" + cluster + "/vpc-cni/" in arn for arn in value["resources"]))
