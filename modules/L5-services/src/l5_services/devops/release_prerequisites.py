"""Fail closed on release-only native eligibility without altering other capabilities."""

INDEXING_UNAVAILABLE = {"status": "unavailable", "source": "devops-agent:GetAssociation",
                        "reason": "native_api_does_not_expose_indexed_revisions"}


def release_association_matches(association, repository, space_id, association_id):
    if not isinstance(association, dict) or not isinstance(repository, dict):
        return False
    configuration = association.get("configuration")
    capabilities = association.get("capabilities")
    if not isinstance(configuration, dict) or not isinstance(capabilities, dict):
        return False
    github = configuration.get("github")
    capability = capabilities.get("RELEASE_READINESS_REVIEW")
    return (isinstance(github, dict) and isinstance(capability, dict)
            and association.get("agentSpaceId") == space_id
            and association.get("associationId") == association_id
            and association.get("status") == "valid"
            and isinstance(association.get("serviceId"), str) and bool(association["serviceId"])
            and isinstance(github.get("owner"), str) and bool(github["owner"])
            and isinstance(github.get("repoName"), str) and bool(github["repoName"])
            and (repository.get("record_id") == github["owner"] + "/" + github["repoName"]
                 or isinstance(github.get("repoId"), str) and bool(github["repoId"])
                 and repository.get("record_id") == github["repoId"])
            and capability.get("enabled") is True)


def source_eligible(options, eligibility, space_ref):
    if "source_indexing" in eligibility:
        return (eligibility["source_indexing"] == INDEXING_UNAVAILABLE
                and eligibility.get("source_indexed") is None
                and eligibility.get("indexed_revisions") == []
                and release_association_matches(eligibility.get("native_association"),
                    options.get("repository_ref"), space_ref.get("record_id"), eligibility.get("association_id")))
    revisions = eligibility.get("indexed_revisions")
    return (eligibility.get("source_indexed") is True and isinstance(revisions, list)
            and all(isinstance(revision, str) for revision in revisions)
            and set(revisions) == {options.get("base_revision"), options.get("head_revision")})


def selected_repository(trigger, repository):
    if not isinstance(trigger, dict) or not isinstance(repository, dict):
        return False
    selected = trigger.get("source_refs")
    if not isinstance(selected, list) or len(selected) != 1 or not isinstance(selected[0], dict):
        return False
    return all(selected[0].get(key) == repository.get(key) and isinstance(repository.get(key), str)
               and repository[key] for key in (
                   "tenant_id", "project_id", "source_connection_id", "record_type", "record_id"))



def release_eligible(options, eligibility, scope, connection_ref, space_ref):
    if not isinstance(eligibility, dict) or not isinstance(options, dict):
        return False
    repository = options.get("repository_ref") or {}
    evidence_ref = eligibility.get("eligibility_ref") or {}
    deployment = eligibility.get("deployment_context") or {}
    if (not isinstance(repository, dict) or not isinstance(evidence_ref, dict)
            or not isinstance(deployment, dict)):
        return False
    if (eligibility.get("status") != "ready" or eligibility.get("task_permission") != "allowed"
            or eligibility.get("permission_action") != "CreateBacklogTask"
            or eligibility.get("permission_space_id") != space_ref.get("record_id")
            or not source_eligible(options, eligibility, space_ref)
            or eligibility.get("repository_ref") != repository
            or eligibility.get("base_revision") != options.get("base_revision")
            or eligibility.get("head_revision") != options.get("head_revision")
            or evidence_ref.get("record_type") != "native_eligibility"
            or evidence_ref.get("tenant_id") != scope.get("tenant_id")
            or not evidence_ref.get("record_id") or not evidence_ref.get("record_revision")
            or evidence_ref.get("project_id") != scope.get("project_id")
            or evidence_ref.get("source_connection_id") != repository.get("source_connection_id")
            or options.get("eligibility_ref") not in (None, evidence_ref)
            or repository.get("record_type") != "repository_commit"
            or repository.get("record_revision") != options.get("head_revision")
            or repository.get("tenant_id") != scope.get("tenant_id")
            or repository.get("project_id") != scope.get("project_id")
            or space_ref.get("tenant_id") != scope.get("tenant_id")
            or space_ref.get("project_id") != scope.get("project_id")
            or space_ref.get("source_connection_id") != connection_ref.get("source_connection_id")
            or deployment.get("revision") != options.get("head_revision")
            or not isinstance(deployment.get("target"), str) or not deployment["target"]):
        return False
    return all(isinstance(eligibility.get(key), str) and eligibility[key]
               for key in ("association_id", "reference_id", "reference_url"))
