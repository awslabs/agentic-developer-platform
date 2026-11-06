"""Installation-scoped GitHub evidence for the signed webhook sender.

Association is a lower bound, not an alias for repository write permission.
Callers must supply only fields from a signature-verified webhook delivery.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

LEVELS = {"NONE": 0, "CONTRIBUTOR": 1, "COLLABORATOR": 2, "MEMBER": 3, "OWNER": 4}
_API = "https://api.github.com"
_cache: dict[
    tuple[int, int, str, int, str, bool, str], tuple[float, "ActorDecision"]
] = {}


@dataclass(frozen=True)
class ActorDecision:
    allowed: bool
    reason: str
    login: str = ""
    permission: str = ""
    association: str = "NONE"


class EvidenceUnavailableError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _account_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    raw = str(value)
    if not raw.isascii() or not raw.isdigit() or len(raw) > 20:
        return None
    number = int(raw)
    return number if number > 0 else None


def _request(
    path: str,
    token: str,
    deadline: float,
    *,
    method: str = "GET",
    body: dict | None = None,
):
    if not path.startswith("/") or path.startswith("//"):
        raise EvidenceUnavailableError("invalid_path")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvidenceUnavailableError("timeout")
    request = Request(
        _API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(_NoRedirect).open(
            request, timeout=min(remaining, 3)
        ) as response:
            if response.status == 204:
                return 204, None
            return response.status, json.loads(response.read(1024 * 1024 + 1))
    except HTTPError as error:
        if (
            error.code in (401, 403, 429)
            or 300 <= error.code < 400
            or error.code >= 500
        ):
            raise EvidenceUnavailableError("github_unavailable") from error
        return error.code, None


def _app_credentials(deadline: float) -> tuple[str, str]:
    import boto3
    from botocore.config import Config

    environment = os.environ.get("ENVIRONMENT", "dev")
    prefix = f"adp/{environment}/github-app/adp-agent-platform-"
    client = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(
            connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}
        ),
    )
    values = []
    for suffix in ("id", "key"):
        if deadline - time.monotonic() <= 2:
            raise EvidenceUnavailableError("timeout")
        values.append(client.get_secret_value(SecretId=prefix + suffix)["SecretString"])
    return values[0], values[1]


def _verify(
    installation_id: int,
    repository_id: int,
    owner: str,
    name: str,
    sender_id: int,
    floor: str,
    deadline: float,
    require_write: bool,
    signed_association: str,
) -> ActorDecision:
    import jwt

    app_id, key = _app_credentials(deadline)
    if _account_id(app_id) is None:
        raise EvidenceUnavailableError("app_unavailable")
    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": app_id}, key, algorithm="RS256"
    )
    status, installation = _request(
        f"/app/installations/{installation_id}", app_jwt, deadline
    )
    if status != 200 or not isinstance(installation, dict):
        return ActorDecision(False, "installation_invalid")
    account = installation.get("account") or {}
    if (
        _account_id(installation.get("id")) != installation_id
        or _account_id(account.get("id")) is None
        or account.get("type") not in ("User", "Organization")
        or account.get("login", "").casefold() != owner.casefold()
    ):
        return ActorDecision(False, "installation_invalid")
    status, token_body = _request(
        f"/app/installations/{installation_id}/access_tokens",
        app_jwt,
        deadline,
        method="POST",
        body={"repository_ids": [repository_id]},
    )
    if status != 201 or not isinstance(token_body, dict) or not token_body.get("token"):
        raise EvidenceUnavailableError("token_unavailable")
    token = token_body["token"]
    repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    status, repository = _request(repo_path, token, deadline)
    if (
        status != 200
        or not isinstance(repository, dict)
        or _account_id(repository.get("id")) != repository_id
    ):
        return ActorDecision(False, "repository_invalid")
    repo_owner = repository.get("owner") or {}
    if (
        _account_id(repo_owner.get("id")) != _account_id(account.get("id"))
        or repository.get("full_name", "").casefold() != f"{owner}/{name}".casefold()
    ):
        return ActorDecision(False, "repository_invalid")
    status, actor = _request(f"/user/{sender_id}", token, deadline)
    if (
        status != 200
        or not isinstance(actor, dict)
        or _account_id(actor.get("id")) != sender_id
    ):
        return ActorDecision(False, "actor_invalid")
    login = actor.get("login")
    if not isinstance(login, str) or not login or actor.get("type") != "User":
        return ActorDecision(False, "actor_invalid")
    encoded_login = quote(login, safe="")
    status, permission_data = _request(
        f"{repo_path}/collaborators/{encoded_login}/permission", token, deadline
    )
    if status not in (200, 404) or (
        status == 200 and not isinstance(permission_data, dict)
    ):
        return ActorDecision(False, "permission_unavailable")
    permission = permission_data.get("permission") if status == 200 else "none"
    permission_actor = (permission_data.get("user") or {}) if status == 200 else {}
    if status == 200 and _account_id(permission_actor.get("id")) != sender_id:
        return ActorDecision(False, "actor_invalid")
    is_writer = permission in ("admin", "maintain", "write")
    if require_write and not is_writer:
        return ActorDecision(False, "insufficient_permission", login=login)
    level = 2 if is_writer else 0
    if account.get("type") == "User" and _account_id(account.get("id")) == sender_id:
        level = 4
    elif account.get("type") == "Organization" and LEVELS[floor] > max(
        level, LEVELS.get(signed_association, 0)
    ):
        status, _ = _request(
            f"/orgs/{quote(owner, safe='')}/members/{encoded_login}", token, deadline
        )
        if status == 204:
            level = 3
        elif status != 404:
            return ActorDecision(False, "association_unavailable")
    level = max(level, LEVELS.get(signed_association, 0))
    if level < LEVELS[floor] and floor == "CONTRIBUTOR":
        status, commits = _request(
            f"{repo_path}/commits?{urlencode({'author': login, 'per_page': 5})}",
            token,
            deadline,
        )
        if status != 200 or not isinstance(commits, list):
            return ActorDecision(False, "association_unavailable")
        if any(
            _account_id((commit.get("author") or {}).get("id")) == sender_id
            for commit in commits
            if isinstance(commit, dict)
        ):
            level = 1
    if level < LEVELS[floor]:
        return ActorDecision(
            False, "insufficient_association", login=login, permission=permission
        )
    association = next(label for label, value in LEVELS.items() if value == level)
    return ActorDecision(True, "allowed", login, permission, association)


def check_actor(
    *,
    installation_id: object,
    repository_id: object,
    repository_full_name: str,
    sender_id: object,
    floor: str | None,
    timeout: float = 10,
    require_write: bool = True,
    signed_association: str | None = None,
    association_actor_id: object = None,
) -> ActorDecision:
    """Prove actor identity, write permission and the configured association floor.

    Fail closed on malformed signed fields, installation misbinding or API faults.
    Error responses are never cached as authorization.
    """
    if floor is not None and (
        not isinstance(floor, str) or floor.upper() not in LEVELS
    ):
        return ActorDecision(False, "eligibility_policy_invalid")
    minimum = floor.upper() if floor else "NONE"
    install = _account_id(installation_id)
    repo = _account_id(repository_id)
    actor = _account_id(sender_id)
    if (
        not install
        or not repo
        or not actor
        or not isinstance(repository_full_name, str)
    ):
        return ActorDecision(False, "actor_evidence_invalid")
    parts = repository_full_name.split("/")
    if len(parts) != 2 or any(not part or part in (".", "..") for part in parts):
        return ActorDecision(False, "actor_evidence_invalid")
    signed = (
        signed_association.upper()
        if (
            _account_id(association_actor_id) == actor
            and isinstance(signed_association, str)
            and signed_association.upper() in LEVELS
        )
        else "NONE"
    )
    cache_key = (
        install,
        repo,
        repository_full_name.casefold(),
        actor,
        minimum,
        require_write,
        signed,
    )
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        decision = _verify(
            install,
            repo,
            parts[0],
            parts[1],
            actor,
            minimum,
            time.monotonic() + min(timeout, 10),
            require_write,
            signed,
        )
    except Exception:
        return ActorDecision(False, "actor_evidence_unavailable")
    if decision.allowed:
        if len(_cache) >= 512:
            _cache.clear()
        _cache[cache_key] = (time.monotonic() + 60, decision)
    return decision
