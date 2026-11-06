"""Project Product repositories to exact WA commit references without advancing pins."""

import re
from copy import deepcopy


def pinned_repository(reference):
    return isinstance(reference.get("record_revision"), str) and re.fullmatch(r"[a-f0-9]{40}", reference["record_revision"]) is not None


def canonical_source(reference):
    reference = deepcopy(reference)
    if isinstance(reference, dict) and reference.get("record_type") == "repository" and pinned_repository(reference):
        reference["record_type"] = "repository_commit"
    return reference


def canonical_sources(references):
    return [canonical_source(reference) for reference in references] if isinstance(references, list) else deepcopy(references)


def repository_pin_missing(references):
    return any(isinstance(reference, dict) and reference.get("record_type") in {"repository", "repository_commit"}
               and not pinned_repository(reference) for reference in references)


def canonical_choices(choices):
    """Keep saved selections, reasoned overrides and trigger scope on the same pin."""
    choices = deepcopy(choices)
    if "source_refs" in choices:
        choices["source_refs"] = canonical_sources(choices["source_refs"])
    for override in choices.get("overrides", []) if isinstance(choices.get("overrides", []), list) else []:
        if isinstance(override, dict) and override.get("field") == "source_refs" and "value" in override:
            override["value"] = canonical_sources(override["value"])
    for trigger in choices.get("triggers", []) if isinstance(choices.get("triggers", []), list) else []:
        if isinstance(trigger, dict) and "source_refs" in trigger:
            trigger["source_refs"] = canonical_sources(trigger["source_refs"])
    return choices
