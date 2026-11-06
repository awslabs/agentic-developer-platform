"""Explicit provenance for published repair heads preserved by a real squash merge."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .maintenance_adoption_provider import require, verified_checks

SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
CONTRACT = "squashed-repair-history/v1"


def tree_sha(value):
    for key in ("commit", "tree", "sha"):
        value = value.get(key) if isinstance(value, dict) else None
    return value


class SquashedRepairHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pull_number: int = Field(strict=True, gt=0)
    reviewed_head: SHA
    merge_revision: SHA
    published_heads: list[SHA] = Field(min_length=1, max_length=1000)


def publication_ids(publications, repository, pull_number, heads):
    matching = [p for p in publications if (p.get("detail") or {}).get("head") in heads]
    require(
        matching
        and {(p.get("detail") or {}).get("head") for p in matching} == set(heads)
        and all(p.get("receipt") == f"github/{repository}/pull/{pull_number}" for p in matching),
        "squash_publication_mismatch",
    )
    return sorted(p["id"] for p in matching)


async def verify_squashed_history(provider, binding, request, release, publications):
    proofs, covered, pulls = [], set(), set()
    for item in request.squashed_repair_history:
        heads = set(item.published_heads)
        require(
            len(heads) == len(item.published_heads) and not covered.intersection(heads) and item.pull_number not in pulls,
            "squash_history_ambiguous",
        )
        ids = publication_ids(publications, binding.repo, item.pull_number, heads)
        pr = (await provider.request(binding, "GET", f"/repos/{binding.repo}/pulls/{item.pull_number}")).json()
        require(
            pr.get("number") == item.pull_number
            and pr.get("merged") is True
            and pr.get("merge_commit_sha") == item.merge_revision
            and pr.get("head", {}).get("sha") == item.reviewed_head
            and pr.get("head", {}).get("repo", {}).get("id") == binding.provider_repository_id
            and pr.get("base", {}).get("repo", {}).get("id") == binding.provider_repository_id
            and pr.get("base", {}).get("ref") == release.default_branch,
            "squash_merge_changed",
        )
        head = (await provider.request(binding, "GET", f"/repos/{binding.repo}/commits/{item.reviewed_head}")).json()
        merge = (await provider.request(binding, "GET", f"/repos/{binding.repo}/commits/{item.merge_revision}")).json()
        parents = merge.get("parents", [])
        head_tree, merge_tree = tree_sha(head), tree_sha(merge)
        require(
            head.get("sha") == item.reviewed_head
            and merge.get("sha") == item.merge_revision
            and item.reviewed_head != item.merge_revision
            and isinstance(parents, list)
            and len(parents) == 1
            and isinstance(parents[0], dict)
            and isinstance(parents[0].get("sha"), str)
            and re.fullmatch(r"[a-f0-9]{40}", parents[0]["sha"]),
            "squash_shape_unverified",
        )
        require(
            isinstance(head_tree, str) and re.fullmatch(r"[a-f0-9]{40}", head_tree) and head_tree == merge_tree,
            "squash_tree_mismatch",
        )
        for source in sorted(heads | {parents[0]["sha"]}):
            if source != item.reviewed_head:
                await provider.contains(binding, source, item.reviewed_head)
        if item.merge_revision != request.source_revision:
            await provider.contains(binding, item.merge_revision, request.source_revision)
        checks = await verified_checks(provider, binding, item.reviewed_head, request.required_checks)
        proofs.append(
            {
                "contract": CONTRACT,
                "repository_id": binding.provider_repository_id,
                "pull_number": item.pull_number,
                "reviewed_head": item.reviewed_head,
                "merge_revision": item.merge_revision,
                "merge_parent": parents[0]["sha"],
                "head_tree": head_tree,
                "merge_tree": merge_tree,
                "published_heads": sorted(heads),
                "publication_ids": ids,
                "required_checks": checks,
            }
        )
        covered.update(heads)
        pulls.add(item.pull_number)
    return proofs, covered


def validate_squash_receipts(proof, history, repository):
    """Retain an explicit bridge for every covered publication on later readback."""
    publications = [p for p in history if p["kind"] == "operations_repair_pr"]
    published = {(p.get("detail") or {}).get("head") for p in publications}
    require(proof["included_published_heads"] == sorted(published), "receipt_history_changed")
    covered, pulls = set(), set()
    for item in proof.get("squashed_repair_history", []):
        heads = item["published_heads"]
        require(
            item["contract"] == CONTRACT
            and item["repository_id"] == proof["repository_id"]
            and heads == sorted(set(heads))
            and not covered.intersection(heads)
            and item["pull_number"] not in pulls
            and item["head_tree"] == item["merge_tree"]
            and all(re.fullmatch(r"[a-f0-9]{40}", item[k]) for k in ("head_tree", "reviewed_head", "merge_revision", "merge_parent"))
            and item["reviewed_head"] != item["merge_revision"]
            and item["publication_ids"] == publication_ids(publications, repository, item["pull_number"], heads),
            "squash_receipt_unverified",
        )
        checks = item["required_checks"]
        require(
            {c["name"] for c in checks} == {c["name"] for c in proof["required_checks"]}
            and all(c["head_sha"] == item["reviewed_head"] and c["status"] == "completed" and c["conclusion"] == "success" for c in checks),
            "squash_receipt_unverified",
        )
        covered.update(heads)
        pulls.add(item["pull_number"])
