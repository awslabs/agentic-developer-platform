"""Read real Product provenance and parse pinned declarative architecture bytes.

This collector performs GET requests only. It does not accept a project-kind,
IaC-format, code-change, or pass flag as evidence. JSON CloudFormation and
Terraform configurations have bounded structural readers; other formats remain
explicitly unqualified instead of being inferred from their filename.
"""

from datetime import UTC, datetime
from hashlib import sha256
import io
import json
import re
from urllib.parse import quote
import zipfile

from l5_services.adapters.well_architected.source_evidence import read_source_files


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def architecture(content):
    """Inspect data only; never run IaC, interpolate it, or resolve modules."""
    if len(content.encode()) > 8 * 1024 * 1024:
        raise ValueError("portable architecture exceeds the source bound")
    try:
        document = json.loads(content)
    except (ValueError, RecursionError) as error:
        raise ValueError("portable architecture needs a pinned CloudFormation or Terraform JSON source; this collector cannot parse other formats") from error
    if not isinstance(document, dict):
        raise ValueError("portable architecture must be a declarative object")
    if isinstance(document.get("Resources"), dict) and document["Resources"]:
        resources = document["Resources"]
        if (len(resources) > 1000 or any(not isinstance(value, dict)
                or not re.fullmatch(r"[A-Za-z0-9]+::[A-Za-z0-9]+::[A-Za-z0-9]+", str(value.get("Type", "")))
                for value in resources.values())):
            raise ValueError("portable CloudFormation resource structure is invalid")
        kind = "cloudformation-json"
    elif isinstance(document.get("resource"), dict) and document["resource"]:
        resources = document["resource"]
        if (len(resources) > 1000 or any(not re.fullmatch(r"[a-z][a-z0-9_]+", name)
                or not isinstance(values, dict) or not values
                or any(not isinstance(value, dict) for value in values.values())
                for name, values in resources.items())):
            raise ValueError("portable Terraform resource structure is invalid")
        kind = "terraform-json"
    else:
        raise ValueError("portable architecture has no supported declarative resources")
    # Canonical structure, not formatting or a user-supplied 'distinct' claim.
    return {"format": kind, "structure_sha256": digest(document), "resources_sha256": digest(resources)}


def binding_rows(client, scope):
    path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/bindings")
    rows, cursor, seen = [], None, set()
    for _ in range(20):
        response = client.get(path, params={"limit": 100, **({"cursor": cursor} if cursor else {})})
        response.raise_for_status()
        page = response.json()
        values = page.get("items")
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(row, dict) for row in values):
            raise ValueError("Product source provenance is incomplete")
        rows.extend(values)
        cursor = page.get("next_cursor")
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("Product source provenance pagination is incomplete")


def selected_binding(rows, case):
    reference = case["architecture"]
    matches = []
    for row in rows:
        selection = row.get("selection") or {}
        if (row.get("project_id") == reference["project_id"]
                and selection.get("kind") == "repository" and selection.get("provider") == "github"
                and selection.get("native_id") == reference["record_id"]
                and selection.get("connection_ref") == {"kind": "github_installation", "id": reference["source_connection_id"]}):
            matches.append(row)
    if len(matches) != 1:
        raise ValueError("portable architecture requires one current Product GitHub source binding")
    row = matches[0]
    observed, stale = (datetime.fromisoformat(row[field].replace("Z", "+00:00")) for field in ("observed_at", "stale_after"))
    if (row.get("status") != "healthy" or row.get("source_revision") != reference["record_revision"]
            or observed.tzinfo is None or stale.tzinfo is None or not observed <= datetime.now(UTC) < stale
            or not re.fullmatch(r"[1-9][0-9]{0,19}", reference["record_id"])
            or not re.fullmatch(r"[a-f0-9]{40}", reference["record_revision"])):
        raise ValueError("portable source provenance is stale or differs from the retained commit")
    return row


def file_record(files, reference, path):
    matches = [item for item in files.values() if item["entry"].get("source_ref") == reference
               and item["entry"].get("path") == path]
    if len(matches) != 1 or matches[0]["entry"].get("revision") != reference["record_revision"]:
        raise ValueError("portable file is absent from the independently verified source archive")
    return matches[0]


def collect(selection, case, native, product_factory):
    paths = selection["qualification_portability"]
    if (not isinstance(paths, dict) or set(paths) != {"architecture_path", "goal_path"}
            or any(not isinstance(value, str) or not value or len(value) > 1024 for value in paths.values())
            or case["architecture"].get("record_type") != "repository_commit"
            or case["goal"].get("record_type") != "document"
            or case["goal"] not in case["settings"].get("goal_refs", [])):
        raise ValueError("portable collection requires selected repository architecture and pinned document goal paths")
    manifest = case["source_manifest"]
    archived = native.read_archive(manifest["provider_source_uri"], manifest["archive"])
    files = read_source_files(manifest, lambda uri, reference: archived)
    iac = file_record(files, case["architecture"], paths["architecture_path"])
    goal = file_record(files, case["goal"], paths["goal_path"])
    with product_factory(selection) as client:
        before = selected_binding(binding_rows(client, selection["scope"]), case)
        contents = {}
        with zipfile.ZipFile(io.BytesIO(archived)) as archive:
            for name, item in (("architecture", iac), ("goal", goal)):
                if item["entry"]["bytes"] > 32768:
                    raise ValueError("portable evidence requires selected declarative and goal files of at most 32 KiB each")
                contents[name] = archive.read(item["entry"]["archive_path"]).decode("utf-8")
        parsed = architecture(contents["architecture"])
        normalized_goal = " ".join(contents["goal"].split())
        if not normalized_goal:
            raise ValueError("portable goal document is empty")
        after = selected_binding(binding_rows(client, selection["scope"]), case)
        if before != after:
            raise ValueError("Product source provenance changed during portability collection")
    return {"source": "independent-product-source-read", "run_id": case["run_id"],
            "result_revision": case["result_revision"], "scope": case["result"]["scope"],
            "binding": before, "manifest_sha256": digest(case["source_manifest"]),
            "architecture": {"entry": iac["entry"], "content_utf8": contents["architecture"], **parsed},
            "goal": {"entry": goal["entry"], "content_utf8": contents["goal"],
                     "normalized_sha256": sha256(normalized_goal.encode()).hexdigest()}}


def verify(cases, observations, installed, context):
    """Bind collected provenance to both exact native cases and release checkpoints."""
    from qualification_installed_artifacts import case_identity

    if (type(context.get("repository_id")) is not int or context["repository_id"] < 1
            or installed.get("case_identities") != [case_identity(case) for case in cases]
            or len(installed.get("snapshot_sha256", [])) != 3):
        raise ValueError("portable proof requires the workflow's platform repository identity and three installed checkpoints")
    repositories, projects, resources, goals = set(), set(), set(), set()
    for case, observation in zip(cases, observations, strict=True):
        proof = observation.get("portability")
        if (not isinstance(proof, dict) or proof.get("source") != "independent-product-source-read"
                or proof.get("run_id") != case["run_id"] or proof.get("result_revision") != case["result_revision"]
                or proof.get("scope") != case["result"]["scope"] or proof.get("manifest_sha256") != digest(case["source_manifest"])):
            raise ValueError("portable evidence is incomplete or belongs to another native case")
        selected_binding([proof["binding"]], case)
        repository = int(case["architecture"]["record_id"])
        if repository == context["repository_id"]:
            raise ValueError("the platform repository cannot qualify a non-ADP project")
        for name, reference in (("architecture", case["architecture"]), ("goal", case["goal"])):
            entry = proof[name]["entry"]
            content = proof[name]["content_utf8"].encode()
            if (entry not in case["source_manifest"]["architecture"]["files"]
                    or entry.get("source_ref") != reference or entry.get("revision") != reference["record_revision"]
                    or len(content) != entry.get("bytes") or not 0 < len(content) <= 32768
                    or sha256(content).hexdigest() != entry.get("sha256")):
                raise ValueError("portable evidence does not name the exact retained source bytes")
        if any(proof["architecture"].get(key) != value for key, value in architecture(proof["architecture"]["content_utf8"]).items()):
            raise ValueError("portable architecture structure was changed")
        normalized_goal = " ".join(proof["goal"]["content_utf8"].split())
        if not normalized_goal or sha256(normalized_goal.encode()).hexdigest() != proof["goal"].get("normalized_sha256"):
            raise ValueError("portable goal content was changed")
        repositories.add(repository)
        projects.add((proof["scope"]["tenant_id"], proof["scope"]["project_id"]))
        resources.add(proof["architecture"]["resources_sha256"])
        goals.add(proof["goal"]["normalized_sha256"])
    if any(len(values) != 2 for values in (repositories, projects, resources, goals)):
        raise ValueError("portable qualification requires two different projects, repositories, architectures and goals")
    return {"source": "independent-product-source-read", "case_identities": installed["case_identities"],
            "source_revision": context["source_revision"], "snapshot_sha256": installed["snapshot_sha256"]}
