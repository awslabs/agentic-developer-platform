"""Shared Product configuration, independent of project selection and source health.

Resource identities group existing bindings; they never grant source access. Binding
and relationship revisions remain the authority for execution and Insights lineage.
"""

import hashlib
import json
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.products.models import ProductProjectBinding, ProductSourceBinding, Project

ResourceID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
EnvironmentID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
Section = Literal["repositories", "environments", "backlog", "knowledge"]


class RepositorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    purpose: Literal["product_code", "reference"]
    label: str | None = Field(default=None, min_length=1, max_length=200)


class NamedEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    environment_class: Literal["development", "test", "staging", "production", "other"]
    resource_id: ResourceID
    deployment_target: str = Field(default="", max_length=512)


class SharedProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repositories: dict[ResourceID, RepositorySettings] = Field(default_factory=dict, max_length=200)
    default_repository: ResourceID | None = None
    environments: dict[EnvironmentID, NamedEnvironment | None] = Field(default_factory=dict, max_length=200)
    not_applicable: list[Section] | None = Field(default=None, max_length=4)

    @model_validator(mode="after")
    def distinct_sections(self):
        if self.not_applicable is not None and len(set(self.not_applicable)) != len(self.not_applicable):
            raise ValueError("Sections must be distinct")
        return self


def resource_id(binding):
    identity = [binding.kind, binding.provider, binding.namespace, binding.native_id]
    # GitHub numeric identifiers have historically been stored with leading zeros.
    if binding.provider == "github":
        identity[2:] = [str(int(value)) if value.isascii() and value.isdecimal() else value for value in identity[2:]]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:32]


async def product_bindings(db, product):
    return (
        await db.execute(
            select(ProductSourceBinding, ProductProjectBinding)
            .join(
                ProductProjectBinding,
                (ProductProjectBinding.tenant_id == ProductSourceBinding.tenant_id)
                & (ProductProjectBinding.project_id == ProductSourceBinding.project_id),
            )
            .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
            .where(
                ProductProjectBinding.tenant_id == product.tenant_id,
                ProductProjectBinding.product_id == product.id,
                ProductProjectBinding.valid_to.is_(None),
                Project.archived_at.is_(None),
                ProductSourceBinding.unlinked_at.is_(None),
            )
            .order_by(ProductSourceBinding.created_at, ProductSourceBinding.id)
        )
    ).all()


async def catalogue(db, actor, product, service_grant=None, *, resolve_names=False):
    from src.products.repository_names import numeric_label, qualified_name, repository_names
    from src.products.source_routes import binding_view, collection_connection_access, service_visible_bindings
    from src.shared.models.vault import ChannelTenantMap

    allowed = {b.id for b in await service_visible_bindings(db, service_grant)} if service_grant else set()
    resources = {}
    unnamed = []
    for binding, relationship in await product_bindings(db, product):
        connection = None if actor.account_type == "service" else await collection_connection_access(db, actor, binding, product.id)
        visible = binding.id in allowed if actor.account_type == "service" else connection is not None
        view = binding_view(binding, visible)
        if "selection" not in view:
            continue
        rid = resource_id(binding)
        desired = binding.desired or {}
        label = (
            (binding.observed or {}).get("display_name")
            or desired.get("full_name")
            or desired.get("display_name")
            or desired.get("label")
            or desired.get("name")
            or desired.get("project_name")
        )
        if not label and binding.kind == "repository" and product.delivery_home_repository:
            home = product.delivery_home_repository
            if binding.provider == home.get("provider") and binding.native_id.isdecimal() and int(binding.native_id) == home.get("repository_id"):
                label = home.get("repository")
        github_repository = binding.kind == "repository" and binding.provider == "github"
        if github_repository and numeric_label(label, binding.namespace, binding.native_id):
            label = None
        if resolve_names and github_repository and not qualified_name(label):
            if actor.account_type == "service":
                connection = await db.get(ChannelTenantMap, binding.installation_id) if binding.installation_id else None
            unnamed.append((binding, connection))
        item = resources.setdefault(
            rid,
            {
                "id": rid,
                "kind": binding.kind,
                "provider": binding.provider,
                "label": label or ("Repository name unavailable" if github_repository else f"{binding.namespace}/{binding.native_id}"),
                "namespace": binding.namespace,
                "native_id": binding.native_id,
                "bindings": [],
                "environment": desired.get("environment"),
                "environment_selections": [],
            },
        )
        if github_repository and qualified_name(label) and not qualified_name(item["label"]):
            item["label"] = label
        if desired.get("environment") and desired["environment"] not in item["environment_selections"]:
            item["environment_selections"].append(desired["environment"])
        item["bindings"].append(
            {
                "id": binding.id,
                "project_id": binding.project_id,
                "revision": binding.revision,
                "generation": binding.generation,
                "relationship_id": relationship.id,
                "relationship_revision": relationship.relationship_revision,
                "status": view["status"],
            }
        )
    # Resolve each immutable source once; do not query GitHub while validating writes.
    missing = [(binding, connection) for binding, connection in unnamed if not qualified_name(resources[resource_id(binding)]["label"])]
    names = await repository_names(missing) if missing else {}
    for binding, _ in missing:
        if names.get(binding.id):
            resources[resource_id(binding)]["label"] = names[binding.id]
    return list(resources.values())


def environments(settings, resources):
    result = {}
    for resource in resources:
        for env in resource.get("environment_selections") or [resource.get("environment")]:
            if resource["kind"] == "environment" and env:
                identity = [resource["id"], env["name"], env["environment_class"]]
                eid = "source-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
                result[eid] = {
                    "name": env["name"],
                    "environment_class": env["environment_class"],
                    "resource_id": resource["id"],
                    "deployment_target": "",
                }
    result.update(settings.get("environments", {}))
    allowed = {r["id"] for r in resources if r["kind"] in ("environment", "deployment_application")}
    return {key: value for key, value in result.items() if value and value["resource_id"] in allowed}


async def apply_patch(db, actor, product, patch):
    resources = await catalogue(db, actor, product)
    by_id = {r["id"]: r for r in resources}
    settings = json.loads(json.dumps(product.profile_configuration or {}))
    repositories = settings.setdefault("repositories", {})
    for rid, value in patch.get("repositories", {}).items():
        if by_id.get(rid, {}).get("kind") != "repository":
            raise HTTPException(422, detail={"code": "profile_repository_unavailable"})
        repositories[rid] = value
    if "default_repository" in patch:
        rid = patch["default_repository"]
        if rid is not None and (
            by_id.get(rid, {}).get("kind") != "repository" or repositories.get(rid, {}).get("purpose", "product_code") != "product_code"
        ):
            raise HTTPException(422, detail={"code": "profile_default_must_be_product_code"})
        settings["default_repository"] = rid
    default = settings.get("default_repository")
    if default and repositories.get(default, {}).get("purpose", "product_code") != "product_code":
        settings["default_repository"] = None
    current_environments = environments(settings, resources)
    for eid, value in patch.get("environments", {}).items():
        # Prevent modifying a hidden environment by guessing its ID.
        if eid in settings.get("environments", {}) and settings["environments"][eid] and eid not in current_environments:
            raise HTTPException(404, detail={"code": "not_found"})
        if value is None and eid not in current_environments and settings.get("environments", {}).get(eid, "absent") is not None:
            raise HTTPException(404, detail={"code": "not_found"})
        if value and by_id.get(value["resource_id"], {}).get("kind") not in ("environment", "deployment_application"):
            raise HTTPException(422, detail={"code": "profile_environment_unavailable"})
        settings.setdefault("environments", {})[eid] = value
    named = environments(settings, resources)
    if patch.get("environments") and (len(named) > 200 or len({v["name"].casefold() for v in named.values()}) != len(named)):
        raise HTTPException(422, detail={"code": "profile_environment_names_must_be_unique"})
    if patch.get("not_applicable") is not None:
        settings["not_applicable"] = patch["not_applicable"]
    return settings


def completion(product, resources, settings, named, knowledge_count=None):
    configured = {
        "details": bool(product.name and product.purpose and product.owner_user_id),
        "repositories": any(
            r["kind"] == "repository" and settings.get("repositories", {}).get(r["id"], {}).get("purpose", "product_code") == "product_code"
            for r in resources
        ),
        "environments": bool(named),
        "backlog": any(r["kind"] in ("tracker", "tracker_team") for r in resources),
        "knowledge": any(r["kind"] == "document" for r in resources) or bool(knowledge_count),
    }
    sections = [
        {
            "id": key,
            "status": "not_applicable"
            if key in settings.get("not_applicable", [])
            else "complete"
            if value
            else "unavailable"
            if key == "knowledge" and knowledge_count is None
            else "missing",
        }
        for key, value in configured.items()
    ]
    applicable = sum(s["status"] != "not_applicable" for s in sections)
    complete = sum(s["status"] == "complete" for s in sections)
    return {"sections": sections, "completed": complete, "applicable": applicable, "percentage": round(100 * complete / applicable)}


async def shared_profile(db, actor, product, *, service_grant=None, settings=None, knowledge_count=None):
    from src.products.repository_names import numeric_label

    resources = await catalogue(db, actor, product, service_grant, resolve_names=True)
    settings = product.profile_configuration or {} if settings is None else settings
    repositories = settings.get("repositories", {})
    for resource in resources:
        if resource["kind"] == "repository":
            config = repositories.get(resource["id"], {})
            resource.update(purpose=config.get("purpose", "product_code"), default=resource["id"] == settings.get("default_repository"))
            label = config.get("label")
            if label and not (resource["provider"] == "github" and numeric_label(label, resource["namespace"], resource["native_id"])):
                resource["label"] = label
    named = environments(settings, resources)
    # Completion measures saved configuration, not credential expiry or connection health.
    configured_resources = resources
    if actor.account_type != "service":
        configured_resources = [
            {"id": resource_id(binding), "kind": binding.kind, "environment": (binding.desired or {}).get("environment")}
            for binding, _ in await product_bindings(db, product)
        ]
    configured_environments = environments(settings, configured_resources)
    return {
        "product_id": product.id,
        "revision": product.revision,
        "catalogue_revision": product.revision,
        "resources": [
            {key: value for key, value in resource.items() if key not in {"environment", "environment_selections"}} for resource in resources
        ],
        "environments": [{"id": eid, **value} for eid, value in named.items()],
        "not_applicable": settings.get("not_applicable", []),
        "completion": completion(product, configured_resources, settings, configured_environments, knowledge_count),
    }
