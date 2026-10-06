"""Authenticate a separate maintainer's approval of one saved runtime plan.

The source repository is a maintained trust boundary, not installer input.
Local manifests and review references are proposals; only live GitHub review
and repository permission records can supply the independent approval.
"""

import base64
import hashlib
import json
import re
import stat
from pathlib import Path
from urllib.parse import quote

from .config import Refusal, deployment_identity, require
from .runner import Commands, atomic

REPOSITORY = "aws-e/adp"
REPOSITORY_ID = 1186991269
MANIFEST_DIRECTORY = "docs/runtime-plan-reviews"
MAX_JSON = 65536


def decode_json(raw, *, maximum=MAX_JSON):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate approval JSON field")
            result[key] = value
        return result

    require(len(raw) <= maximum, "Approval document exceeds size bound")
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError):
        raise Refusal("Approval document is invalid JSON") from None


def private_bytes(path, maximum=MAX_JSON):
    path = Path(path)
    require(
        not path.is_symlink() and path.is_file(),
        "Saved runtime review file missing or linked",
    )
    info = path.stat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_mode & 0o077 == 0
        and 0 < info.st_size <= maximum,
        "Saved runtime review file is not private or exceeds size bound",
    )
    return path.read_bytes()


def manifest(environment, operator, directory):
    """Return only bounded nonsecret target references and review digests."""
    directory = Path(directory)
    require(
        not directory.is_symlink() and not (directory / "terraform").is_symlink(),
        "Runtime review directory cannot be linked",
    )
    receipt = decode_json(private_bytes(directory / "runtime-preparation.json"))
    selected = dict(deployment_identity(environment, required=True))
    review_id = operator.get("review_id")
    require(
        isinstance(review_id, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", review_id),
        "Runtime review ID must be a bounded identifier",
    )
    require(
        receipt.get("version") == 1
        and receipt.get("review_id") == review_id
        and isinstance(receipt.get("installation_id"), str)
        and re.fullmatch(r"[0-9a-f]{24}", receipt["installation_id"])
        and receipt.get("worker_ready") is False
        and receipt.get("status") in ("planned", "apply-attempted", "applied"),
        "Runtime preparation receipt is not a reviewable plan",
    )
    for key in ("request_sha256", "proposal_sha256", "source_sha256", "plan_sha256"):
        require(
            isinstance(receipt.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", receipt[key]),
            "Runtime preparation digest is invalid",
        )
    plan = private_bytes(directory / "terraform/installation.tfplan", 32 * 1024 * 1024)
    binary_digest = hashlib.sha256(plan).hexdigest()
    if "binary_plan_sha256" in receipt:
        require(
            receipt["binary_plan_sha256"] == binary_digest,
            "Saved binary differs from runtime preparation receipt",
        )
    return {
        "version": 1,
        "scope": "superplane-domain-runtime-v1",
        "review_id": review_id,
        "installation_id": receipt["installation_id"],
        "request_sha256": receipt["request_sha256"],
        "proposal_sha256": receipt["proposal_sha256"],
        "source_sha256": receipt["source_sha256"],
        "plan_sha256": receipt["plan_sha256"],
        "binary_plan_sha256": binary_digest,
        "target": {
            key: environment[key]
            for key in ("account_id", "region", "environment", "cluster", "namespace")
        },
        "deployment_identity": selected,
        "resources": receipt["resources"],
        "worker_ready": False,
    }


def manifest_path(review_id):
    require(
        isinstance(review_id, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", review_id),
        "Invalid runtime plan review ID",
    )
    return f"{MANIFEST_DIRECTORY}/{review_id}.json"


class GitHubReadAPI:
    def __init__(self, commands=None):
        self.commands = commands or Commands()

    def get(self, path):
        result = self.commands.call(
            ["gh", "api", "--hostname", "github.com", path], timeout=30
        )
        return decode_json(result.stdout, maximum=2 * 1024 * 1024)


class GitHubPlanApproval:
    def __init__(self, review_url, environment, operator, directory, *, api=None):
        match = re.fullmatch(
            r"https://github\.com/aws-e/adp/pull/([1-9][0-9]{0,9})", review_url
        )
        require(match is not None, "Plan review must name an aws-e/adp pull request")
        self.number = int(match[1])
        self.environment, self.operator = environment, operator
        self.directory = Path(directory)
        self.api = api or GitHubReadAPI()
        self.prefix = "repos/" + REPOSITORY

    def pull(self):
        document = self.api.get(f"{self.prefix}/pulls/{self.number}")
        require(
            document.get("number") == self.number
            and document.get("state") == "open"
            and document.get("draft") is False
            and document.get("base", {}).get("ref") == "main"
            and document.get("base", {}).get("repo", {}).get("id") == REPOSITORY_ID
            and document.get("head", {}).get("repo", {}).get("id") == REPOSITORY_ID
            and isinstance(document.get("head", {}).get("sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", document["head"]["sha"])
            and type(document.get("user", {}).get("id")) is int,
            "Runtime plan review must be an open same-repository main PR",
        )
        return document

    def maintainer(self, user):
        login = user.get("login", "")
        require(
            type(user.get("id")) is int
            and isinstance(login, str)
            and re.fullmatch(r"[A-Za-z0-9-]{1,80}(?:\[bot\])?", login),
            "Review principal is invalid",
        )
        permission = self.api.get(
            f"{self.prefix}/collaborators/{quote(login, safe='')}/permission"
        )
        require(
            permission.get("user", {}).get("id") == user["id"],
            "Review permission principal mismatch",
        )
        return permission.get("role_name") in ("maintain", "admin") and permission.get(
            "permission"
        ) in ("write", "admin", "maintain")

    def reviews(self):
        reviews = []
        for page in range(1, 11):
            values = self.api.get(
                f"{self.prefix}/pulls/{self.number}/reviews?per_page=100&page={page}"
            )
            require(isinstance(values, list), "Invalid GitHub review response")
            reviews.extend(values)
            if len(values) < 100:
                return reviews
        raise Refusal("Runtime plan review history exceeds bounded lookup")

    def verify_plan(self, *, plan_sha256, installation_id, review_id):
        expected = manifest(self.environment, self.operator, self.directory)
        require(
            expected["plan_sha256"] == plan_sha256
            and expected["installation_id"] == installation_id
            and expected["review_id"] == review_id,
            "Approval request differs from saved runtime plan",
        )
        repo = self.api.get(self.prefix)
        require(
            repo.get("id") == REPOSITORY_ID
            and repo.get("full_name") == REPOSITORY
            and repo.get("default_branch") == "main",
            "Trusted runtime review repository identity changed",
        )
        pull = self.pull()
        head = pull["head"]["sha"]
        path = manifest_path(review_id)
        contents = self.api.get(f"{self.prefix}/contents/{path}?ref={head}")
        require(
            contents.get("type") == "file"
            and contents.get("path") == path
            and contents.get("encoding") == "base64"
            and type(contents.get("size")) is int
            and 0 < contents["size"] <= MAX_JSON,
            "Reviewed runtime manifest is missing or invalid",
        )
        try:
            encoded = "".join(contents["content"].splitlines())
            require(len(encoded) <= MAX_JSON * 2, "Encoded manifest exceeds bound")
            raw = base64.b64decode(encoded, validate=True)
        except (KeyError, ValueError, TypeError):
            raise Refusal("Reviewed runtime manifest encoding is invalid") from None
        require(
            len(raw) == contents["size"]
            and hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw
            ).hexdigest()
            == contents.get("sha"),
            "Reviewed manifest Git blob identity mismatch",
        )
        require(
            json.dumps(decode_json(raw), sort_keys=True, separators=(",", ":"))
            == json.dumps(expected, sort_keys=True, separators=(",", ":")),
            "Reviewed manifest differs from saved plan",
        )
        latest = {}
        for review in self.reviews():
            require(
                isinstance(review, dict)
                and type(review.get("id")) is int
                and type(review.get("user", {}).get("id")) is int,
                "Invalid GitHub review identity",
            )
            key = review["user"]["id"]
            if key not in latest or review["id"] > latest[key]["id"]:
                latest[key] = review
        approvals = []
        for reviewer_id, review in latest.items():
            if reviewer_id == pull["user"]["id"]:
                continue
            if not self.maintainer(review["user"]):
                continue
            require(
                review.get("state") != "CHANGES_REQUESTED",
                "A trusted runtime plan reviewer requests changes",
            )
            if review.get("state") == "APPROVED" and review.get("commit_id") == head:
                approvals.append(review)
        require(
            approvals, "No separate current-head maintainer approval of runtime plan"
        )
        approved = max(approvals, key=lambda item: item["id"])
        final_pull = self.pull()
        final_review = self.api.get(
            f"{self.prefix}/pulls/{self.number}/reviews/{approved['id']}"
        )
        require(
            final_pull["head"]["sha"] == head
            and final_pull["user"]["id"] == pull["user"]["id"]
            and final_review.get("id") == approved["id"]
            and final_review.get("state") == "APPROVED"
            and final_review.get("commit_id") == head
            and final_review.get("user", {}).get("id") == approved["user"]["id"]
            and self.maintainer(final_review["user"]),
            "Runtime plan head, approval or reviewer authority changed",
        )
        require(
            manifest(self.environment, self.operator, self.directory) == expected,
            "Saved runtime plan changed while verifying approval",
        )
        atomic(
            self.directory / "runtime-plan-review-evidence.json",
            {
                "version": 1,
                "repository_id": REPOSITORY_ID,
                "pull_request": self.number,
                "commit_id": head,
                "review_id": approved["id"],
                "reviewer_id": approved["user"]["id"],
                "manifest": expected,
            },
        )
        return {
            "approved": True,
            "plan_sha256": plan_sha256,
            "installation_id": installation_id,
            "review_id": review_id,
            "approver": "github:user:" + str(approved["user"]["id"]),
        }
