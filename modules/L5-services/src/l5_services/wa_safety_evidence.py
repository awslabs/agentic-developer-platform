"""Fail-closed readback of installed WA recovery and owned-only cleanup."""

import re
from hashlib import sha256

from l5_services.installed_release_probe import require


def verify_wa_safety(case, fixture, read, absent):
    require(isinstance(case, dict) and case.get("source") == "authenticated-runtime"
            and isinstance(case.get("evidence_ref"), str) and bool(case["evidence_ref"]),
            "wa_safety_private_receipt_missing")
    scope, native = case.get("scope"), case.get("native_job_ref")
    require(isinstance(scope, dict) and all(isinstance(scope.get(field), str) and scope[field]
                                            for field in ("tenant_id", "project_id"))
            and isinstance(native, dict) and native.get("tenant_id") == scope["tenant_id"]
            and native.get("project_id") == scope["project_id"], "wa_safety_scope_changed")
    denials = case.get("denials")
    kinds = {"foreign_tenant", "sibling_project", "foreign_source", "wrong_role"}
    require(isinstance(denials, list) and len(denials) == len(kinds)
            and all(isinstance(item, dict) for item in denials)
            and {item.get("kind") for item in denials} == kinds, "wa_safety_denials_missing")
    references = set()
    for denial in denials:
        request = denial.get("request_scope")
        kind = denial["kind"]
        source_scope = denial.get("source_scope")
        foreign_source = (isinstance(source_scope, dict)
                          and all(isinstance(source_scope.get(field), str) and source_scope[field]
                                  for field in ("tenant_id", "project_id"))
                          and source_scope != scope)
        require(isinstance(request, dict) and all(isinstance(request.get(field), str) and request[field]
                                                  for field in ("tenant_id", "project_id"))
                and ((kind == "foreign_tenant" and request["tenant_id"] != scope["tenant_id"])
                     or (kind == "sibling_project" and request["tenant_id"] == scope["tenant_id"]
                         and request["project_id"] != scope["project_id"])
                     or (kind in {"foreign_source", "wrong_role"} and request == scope))
                and (kind != "foreign_source" or foreign_source)
                and (kind != "wrong_role" or isinstance(denial.get("role_arn"), str)
                     and denial["role_arn"] and denial["role_arn"] != fixture.role_arn)
                and denial.get("status") in {403, 404}
                and denial.get("source") == "installed-worker"
                and isinstance(denial.get("evidence_ref"), str) and denial["evidence_ref"]
                and type(denial.get("provider_calls_before")) is int
                and denial["provider_calls_before"] >= 0
                and denial.get("provider_calls_after") == denial["provider_calls_before"]
                and isinstance(denial.get("native_ids_before"), list)
                and denial.get("native_ids_after") == denial["native_ids_before"],
                "wa_safety_denial_effect_unverified")
        references.add(denial["evidence_ref"])
    require(len(references) == len(kinds), "wa_safety_denials_missing")
    recovery = case.get("recovery")
    require(isinstance(recovery, dict) and recovery.get("source") == "installed-worker"
            and isinstance(recovery.get("evidence_ref"), str) and recovery["evidence_ref"]
            and isinstance(recovery.get("attempt_id"), str) and recovery["attempt_id"]
            and isinstance(recovery.get("run_id"), str) and recovery["run_id"]
            and recovery.get("faults") == ["lost_start_response", "restart", "local_timeout"]
            and type(recovery.get("start_call_count")) is int and recovery["start_call_count"] == 1
            and recovery.get("native_job_ref") == native
            and recovery.get("remote_state_at_timeout") in {"unknown", "running"},
            "wa_safety_recovery_unverified")
    region = fixture.queue_arn.split(":")[3]
    prefix = f"arn:aws:wellarchitected:{region}:{fixture.account_id}:agent-profile/"
    profile, generation_id, name = (native.get(field) for field in ("profile_arn", "generation_id", "name"))
    require(isinstance(profile, str) and profile.startswith(prefix)
            and isinstance(generation_id, str) and generation_id
            and name == "adp-wa-" + sha256(
                f"{scope['tenant_id']}:{recovery['run_id']}:{recovery['attempt_id']}".encode()
            ).hexdigest()[:32],
            "wa_safety_native_identity_unverified")
    adopted, owned = case.get("adopted"), case.get("owned")
    require(isinstance(adopted, dict) and adopted.get("profile_arn") == profile
            and isinstance(adopted.get("before"), dict) and adopted["before"].get("arn") == profile
            and set(adopted["before"]) == {"arn", "executionRoleArn", "pillars",
                                           "aggregationConfiguration", "tags"}
            and isinstance(adopted.get("contexts_before"), list)
            and isinstance(owned, dict) and owned.get("ownership") == "receipt_owned"
            and isinstance(owned.get("evidence_ref"), str) and owned["evidence_ref"]
            and isinstance(owned.get("profile_arn"), str)
            and owned["profile_arn"].startswith(prefix) and owned["profile_arn"] != profile,
            "wa_safety_ownership_unverified")
    archive, result = case.get("archive"), case.get("result")
    require(isinstance(archive, dict) and all(isinstance(archive.get(field), str) and archive[field]
                                              for field in ("bucket", "key", "version"))
            and isinstance(archive.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
            and isinstance(result, dict) and result.get("source") == "authenticated-runtime"
            and result.get("readback_source") == "installed-api"
            and isinstance(result.get("evidence_ref"), str) and result["evidence_ref"]
            and result.get("scope") == scope and result.get("native_job_ref") == native
            and isinstance(result.get("result_id"), str) and result["result_id"]
            and type(result.get("revision")) is int and result["revision"] >= 1
            and isinstance(result.get("before_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", result["before_sha256"])
            and result.get("after_sha256") == result["before_sha256"],
            "wa_safety_retention_unverified")
    token = None
    tokens = set()
    matching = []
    for _ in range(20):
        arguments = ["--profile-arn", profile, "--recommendation-type", "ARCHITECTURE"]
        if token:
            arguments += ["--next-token", token]
        page = read("wellarchitected", "list-agent-recommendation-generations", *arguments)
        require(isinstance(page, dict) and isinstance(page.get("items"), list)
                and all(isinstance(item, dict) for item in page["items"]),
                "wa_safety_native_listing_unverified")
        matching.extend(item.get("id") for item in page["items"]
                        if item.get("name") == name and item.get("profileArn") == profile)
        token = page.get("nextToken")
        if not token:
            break
        require(isinstance(token, str) and token not in tokens, "wa_safety_native_listing_unverified")
        tokens.add(token)
    else:
        require(False, "wa_safety_native_listing_unverified")
    require(matching == [generation_id], "wa_safety_duplicate_generation")
    detail = read("wellarchitected", "get-agent-recommendation-generation",
                  "--profile-arn", profile, "--generation-id", generation_id)
    context = detail.get("additionalContext")
    require(detail.get("id") == generation_id and detail.get("profileArn") == profile
            and detail.get("name") == name and detail.get("status") == "COMPLETED"
            and isinstance(context, dict) and isinstance(context.get("sourceArchive"), dict)
            and context["sourceArchive"].get("sha256") == archive["sha256"],
            "wa_safety_native_identity_unverified")
    head = read("s3api", "head-object", "--bucket", archive["bucket"],
                "--key", archive["key"], "--version-id", archive["version"])
    require(head.get("VersionId") == archive["version"]
            and type(head.get("ContentLength")) is int and head["ContentLength"] > 0
            and (not isinstance(head.get("Metadata"), dict)
                 or head["Metadata"].get("sha256", archive["sha256"]) == archive["sha256"]),
            "wa_safety_source_missing")
    after = read("wellarchitected", "get-agent-profile", "--profile-arn", profile)
    require(all(after.get(key) == value for key, value in adopted["before"].items()),
            "wa_safety_adopted_changed")
    token = None
    tokens = set()
    contexts = []
    for _ in range(20):
        arguments = ["--profile-arn", profile]
        if token:
            arguments += ["--next-token", token]
        page = read("wellarchitected", "list-agent-contexts", *arguments)
        require(isinstance(page, dict) and isinstance(page.get("items"), list)
                and all(isinstance(item, dict) and item.get("profileArn") == profile
                        for item in page["items"]), "wa_safety_adopted_changed")
        contexts.extend(page["items"])
        token = page.get("nextToken")
        if not token:
            break
        require(isinstance(token, str) and token not in tokens, "wa_safety_adopted_changed")
        tokens.add(token)
    else:
        require(False, "wa_safety_adopted_changed")
    require(contexts == adopted["contexts_before"], "wa_safety_adopted_changed")
    require(absent("wellarchitected", "get-agent-profile", "--profile-arn", owned["profile_arn"]),
            "wa_safety_owned_present")
    token = None
    tokens = set()
    profiles = []
    for _ in range(20):
        arguments = ("--next-token", token) if token else ()
        page = read("wellarchitected", "list-agent-profiles", *arguments)
        require(isinstance(page, dict) and isinstance(page.get("items"), list)
                and all(isinstance(item, dict) for item in page["items"]),
                "wa_safety_profile_listing_unverified")
        profiles.extend(item.get("arn") for item in page["items"])
        token = page.get("nextToken")
        if not token:
            break
        require(isinstance(token, str) and token not in tokens, "wa_safety_profile_listing_unverified")
        tokens.add(token)
    else:
        require(False, "wa_safety_profile_listing_unverified")
    require(profile in profiles and owned["profile_arn"] not in profiles,
            "wa_safety_owned_or_adopted_unverified")
    return {"native_id": generation_id, "result_id": result["result_id"]}
