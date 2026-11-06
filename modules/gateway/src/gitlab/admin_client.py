"""GitLab administration for the isolated home reconciler only."""

import hashlib
import re
import ssl
from urllib.parse import quote

import httpx

from src.gitlab.config import host


class ProvisioningError(Exception):
    """Safe operator error code; never include provider bodies or credentials."""


class GitlabAdmin:
    def __init__(self, url, token, *, transport=None, ca_file=None):
        try:
            verification = ssl.create_default_context(cafile=ca_file) if ca_file is not None else True
        except (OSError, ValueError):
            raise ProvisioningError("gitlab_ca_not_verified") from None
        self.http = httpx.AsyncClient(
            base_url=host(url) + "/api/v4/",
            headers={"PRIVATE-TOKEN": token},
            follow_redirects=False,
            trust_env=False,
            timeout=15,
            transport=transport,
            verify=verification,
        )
        self.owner_id = None

    async def __aenter__(self):
        try:
            user = await self.request("GET", "user")
            if not isinstance(user, dict) or not user.get("is_admin") or type(user.get("id")) is not int:
                raise ProvisioningError("admin_credential_required")
            self.owner_id = user["id"]
            return self
        except Exception:
            await self.http.aclose()
            raise

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def request(self, method, path, *, missing=False, **kwargs):
        if (
            not isinstance(path, str)
            or not path
            or not path[0].isalnum()
            or not re.fullmatch(r"[A-Za-z0-9._/%~-]+", path)
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise ProvisioningError("gitlab_path_not_verified")
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.RequestError:
            raise ProvisioningError("gitlab_transport_failure") from None
        if response.status_code == 404 and missing:
            return None
        if response.status_code not in (200, 201, 202, 204):
            raise ProvisioningError(f"gitlab_http_{response.status_code}")
        try:
            return response.json() if response.content else None
        except ValueError:
            raise ProvisioningError("invalid_gitlab_response") from None

    async def pages(self, path, **params):
        rows = []
        for page in range(1, 101):
            batch = await self.request("GET", path, params={**params, "page": page, "per_page": 100})
            if not isinstance(batch, list):
                raise ProvisioningError("invalid_gitlab_response")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ProvisioningError("pagination_limit")

    async def group(self, path, name, marker, *, parent_id=None):
        full_path = path if parent_id is None else f"{parent_id[1]}/{path}"
        group = await self.request("GET", f"groups/{quote(full_path, safe='')}", missing=True)
        if group is None:
            try:
                group = await self.request(
                    "POST",
                    "groups",
                    json={
                        "path": path,
                        "name": name,
                        "description": marker,
                        "visibility": "private",
                        "parent_id": parent_id[0] if parent_id else None,
                        "subgroup_creation_level": "owner",
                        "project_creation_level": "maintainer",
                        "share_with_group_lock": True,
                    },
                )
            except ProvisioningError:
                # A lost response or concurrent creator can leave the resource present.
                group = await self.request("GET", f"groups/{quote(full_path, safe='')}", missing=True)
                if group is None:
                    raise
        if group.get("description") != marker or group.get("full_path") != full_path:
            raise ProvisioningError("namespace_ownership_conflict")
        owners = await self.pages(f"groups/{group['id']}/members")
        if not any(row["id"] == self.owner_id and row["access_level"] == 50 for row in owners):
            raise ProvisioningError("namespace_ownership_conflict")
        await self.request(
            "PUT",
            f"groups/{group['id']}",
            json={
                "name": name,
                "visibility": "private",
                "share_with_group_lock": True,
                "subgroup_creation_level": "owner",
                "project_creation_level": "maintainer",
            },
        )
        for shared in group.get("shared_with_groups", []):
            await self.request("DELETE", f"groups/{group['id']}/share/{shared['group_id']}", missing=True)
        return group

    async def user(self, identity):
        users = await self.pages("users", provider="jwt", extern_uid=identity["id"])
        if not users:
            # Link only through a proven immutable Cognito subject, never email matching.
            users = await self.pages("users", provider="openid_connect", extern_uid=identity["subject"])
            if len(users) == 1 and not users[0].get("is_admin"):
                await self.request("PUT", f"users/{users[0]['id']}", json={"provider": "jwt", "extern_uid": identity["id"], "external": True})
            elif not users:
                try:
                    created = await self.request(
                        "POST",
                        "users",
                        json={
                            "email": identity["email"],
                            "name": identity["name"],
                            "username": "adp-" + hashlib.sha256(identity["id"].encode()).hexdigest()[:32],
                            "provider": "jwt",
                            "extern_uid": identity["id"],
                            "force_random_password": True,
                            "skip_confirmation": True,
                            "external": True,
                            "can_create_group": False,
                        },
                    )
                    users = [created]
                except ProvisioningError:
                    users = await self.pages("users", provider="jwt", extern_uid=identity["id"])
                    if not users:
                        raise ProvisioningError("identity_link_required") from None
        if len(users) != 1 or users[0].get("is_admin") or users[0].get("state") != "active":
            raise ProvisioningError("identity_not_active")
        # Read back the identity after create/link before granting any membership.
        verified = await self.pages("users", provider="jwt", extern_uid=identity["id"])
        if len(verified) != 1 or verified[0]["id"] != users[0]["id"]:
            raise ProvisioningError("identity_not_verified")
        await self.request("PUT", f"users/{users[0]['id']}", json={"external": True, "can_create_group": False})
        return users[0]["id"]

    async def memberships(self, kind, resource_id, desired):
        path = f"{kind}/{resource_id}/members"
        actual = {row["id"]: row for row in await self.pages(path)}
        for user_id, row in actual.items():
            if user_id == self.owner_id:
                continue
            if user_id not in desired:
                await self.request("DELETE", f"{path}/{user_id}", missing=True)
            elif row["access_level"] != desired[user_id]:
                await self.request("PUT", f"{path}/{user_id}", json={"access_level": desired[user_id]})
        for user_id, level in desired.items():
            if user_id not in actual:
                await self.request("POST", path, json={"user_id": user_id, "access_level": level})
        verified = {row["id"]: row["access_level"] for row in await self.pages(path) if row["id"] != self.owner_id}
        if verified != desired:
            raise ProvisioningError("membership_not_verified")

    async def project_boundaries(self, group_id):
        # Product members inherit access from the subgroup. Remove unmanaged direct
        # project grants and group shares, which would otherwise survive offboarding.
        for project in await self.pages(f"groups/{group_id}/projects", include_subgroups="true", with_shared="false"):
            await self.request("PUT", f"projects/{project['id']}", json={"visibility": "private"})
            await self.memberships("projects", project["id"], {})
            # The group project listing can omit details under API optimizations.
            details = await self.request("GET", f"projects/{project['id']}")
            for share in details.get("shared_with_groups", []):
                await self.request("DELETE", f"projects/{project['id']}/share/{share['group_id']}", missing=True)

    async def effective_memberships(self, kind, resource_id, desired):
        actual = {row["id"]: row["access_level"] for row in await self.pages(f"{kind}/{resource_id}/members/all") if row["id"] != self.owner_id}
        if actual != desired:
            raise ProvisioningError("inherited_membership_not_verified")

    async def descendants(self, group_id):
        # Older homes may contain subgroups even though ordinary members can no
        # longer create them. Remove grants/shares there before offboarding can
        # be considered complete; all human authority inherits from the Product.
        for child in await self.pages(f"groups/{group_id}/descendant_groups"):
            details = await self.request("GET", f"groups/{child['id']}")
            await self.request(
                "PUT",
                f"groups/{child['id']}",
                json={
                    "visibility": "private",
                    "share_with_group_lock": True,
                    "subgroup_creation_level": "owner",
                    "project_creation_level": "maintainer",
                },
            )
            await self.memberships("groups", child["id"], {})
            for share in details.get("shared_with_groups", []):
                await self.request("DELETE", f"groups/{child['id']}/share/{share['group_id']}", missing=True)
