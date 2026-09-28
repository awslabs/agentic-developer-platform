"""Source admission shared by ingestion and the separately packaged gateway.

The URL denylist is the existing URL-analysis validator, pinned by drift tests.
Admission does not replace destination pinning and ownership checks at fetch.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from s3_source_guard import check_s3_source
from scope import IngestionScope, parse_scope
from url_denylist import check_url, normalize_backslashes


class SourceAdmissionError(ValueError):
    """The source cannot be admitted for the verified scope."""


def validate_source(
    asset_type: str,
    source: str,
    scope: IngestionScope,
    *,
    default_bucket: str = "",
    allowlist: str = "",
    allow_infra: bool = False,
) -> None:
    scope = parse_scope(scope.to_dict())
    if not isinstance(source, str) or not source:
        raise SourceAdmissionError("source is required")
    if asset_type == "repo":
        candidate = source.removeprefix("https://github.com/").removeprefix("git@github.com:")
        candidate = candidate.rstrip("/").removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", candidate):
            raise SourceAdmissionError("repository must identify exactly one GitHub owner/repository")
        if candidate.split("/")[1] in {".", ".."}:
            raise SourceAdmissionError("repository path is invalid")
        return  # GitHub visibility/installation authorization is a separate required step.
    if asset_type == "url" or (asset_type == "doc" and source.startswith(("http://", "https://"))):
        try:
            parsed = urlsplit(normalize_backslashes(source))
            if parsed.username is not None or parsed.password is not None:
                raise SourceAdmissionError("inline URL credentials are not accepted")
            parsed.port  # Reject malformed/out-of-range ports before registration.
        except ValueError as exc:
            raise SourceAdmissionError("invalid HTTP source authority") from exc
        decision = check_url(source)
    elif asset_type == "doc":
        decision = check_s3_source(source, allowlist, default_bucket, scope=scope)
    elif asset_type == "infra" and allow_infra:
        # Internal inventory publisher, not a customer-registerable asset type.
        if re.fullmatch(r"[0-9]{12}(?::[A-Za-z0-9+=,.@_-]+(?::[a-z0-9,-]+)?)?", source):
            return
        raise SourceAdmissionError("infrastructure source must be an AWS account identifier")
    else:
        raise SourceAdmissionError("asset type has no source admission validator")
    if not decision.allowed:
        raise SourceAdmissionError(decision.reason_code)
