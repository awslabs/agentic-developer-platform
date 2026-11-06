"""Validate the immutable repository selection used for code review."""

from l5_services.source_manifest import SourceIntegrityError


def verify_review_selection(run):
    sources = run.input.get("sources", [])
    if (not 1 <= len(sources) <= 2
            or any(source.get("record_type") != "repository_commit" for source in sources)):
        raise SourceIntegrityError("code review requires pinned repository commits")

    repository = sources[0]
    identity = ("tenant_id", "project_id", "source_connection_id", "record_id")
    if (repository.get("tenant_id") != run.tenant_id
            or repository.get("project_id") != run.project_id
            or any(not repository.get(field) for field in identity)
            or any(any(source.get(field) != repository[field] for field in identity)
                   for source in sources)):
        raise SourceIntegrityError("code review repository scope mismatch")

    base, head = run.input.get("pr_base"), run.input.get("pr_head")
    revisions = [source.get("record_revision") for source in sources]
    if (any(not isinstance(revision, str) or not revision
            or revision.casefold() in {"head", "latest", "null"} for revision in revisions)
            or (base is None) != (head is None)
            or (base is None and len(sources) != 1)
            or (base is not None and revisions != [base, head])):
        raise SourceIntegrityError("code review revision mismatch")


def verify_review_event(run):
    admission = run.admission
    event_id = admission.get("event_id")
    kind = admission.get("event_kind")
    source = admission.get("event_source_ref")
    if event_id is None:
        valid = kind in {None, "manual"} and source is None
    elif not isinstance(event_id, str) or not event_id:
        valid = False
    elif kind == "pull_request":
        valid = source == run.input["sources"][-1]
    elif kind == "schedule":
        valid = source is None and event_id.startswith("schedule:")
    else:
        valid = False
    if not valid:
        raise SourceIntegrityError("code review event does not match pinned source")
