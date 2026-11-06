"""Fixed, bounded source reads; source credentials never leave the broker."""

import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.vault_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.connector_context_routes import read_context
from src.internal.connector_work_authority import get_connector_work_authorizer
from src.products.models import ProductOperation, ProductSourceBinding
from src.shared.database import get_db
from src.shared.models.vault import ChannelTenantMap, UserCredential

SITE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.atlassian\.net\Z")
NATIVE_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
MAX_RESPONSE = 8 * 1024 * 1024
router = APIRouter(prefix="/internal/v1/connector-contexts", tags=["collection-broker"], dependencies=[Depends(verify_internal_or_irsa)])


async def jira_auth(credential, secrets):
    if credential is None or (credential.provider or credential.service) != "jira" or credential.credential_type != "basic_auth":
        raise HTTPException(409, detail={"code": "unsupported_jira_credential"})
    try:
        secret = json.loads(await asyncio.to_thread(secrets.get_secret, credential.secret_arn))
        site, email, token = secret["url"], secret["email"], secret["token"]
        if not SITE.fullmatch(site) or not isinstance(email, str) or not email or not isinstance(token, str) or not token:
            raise ValueError()
    except Exception:
        raise HTTPException(409, detail={"code": "jira_credential_unavailable"}) from None
    return site, httpx.BasicAuth(email, token)


async def bounded_read(client, url, **kwargs):
    try:
        async with client.stream("GET", url, **kwargs) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_RESPONSE:
                    raise ValueError()
                data.extend(chunk)
            return bytes(data), response.headers
    except (httpx.HTTPError, ValueError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None


async def jira_boards(credential, secrets, *, page=1, limit=100, board_id=None, client=None):
    site, auth = await jira_auth(credential, secrets)
    if board_id is not None and not NATIVE_ID.fullmatch(board_id):
        raise HTTPException(422, detail={"code": "invalid_source"})
    path = "/rest/agile/1.0/board" + ("/" + board_id if board_id else "")
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
    try:
        payload, _ = await bounded_read(
            client, site + path, auth=auth, params={} if board_id else {"startAt": (page - 1) * limit, "maxResults": limit}
        )
        result = json.loads(payload)
        if board_id:
            if str(result.get("id")) != board_id:
                raise ValueError()
            rows, complete = [result], True
        else:
            rows, complete = result["values"], result["isLast"]
            if not isinstance(rows, list) or len(rows) > limit or type(complete) is not bool or result["startAt"] != (page - 1) * limit:
                raise ValueError()
            if not complete and len(rows) != limit:
                raise ValueError()
        resources = []
        for row in rows:
            native_id = str(row.get("id"))
            if not NATIVE_ID.fullmatch(native_id) or not isinstance(row.get("name"), str):
                raise ValueError()
            resources.append(
                {
                    "kind": "tracker",
                    "namespace": site,
                    "native_id": native_id,
                    "name": row["name"],
                    "source_revision": hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                }
            )
        return resources, None if complete else str(page + 1), "complete" if complete else "partial"
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def sonar_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.expanded_provider import material

    if (
        kind not in ("project", "issue")
        or kind == "project"
        and page != 1
        or not isinstance(binding.native_id, str)
        or not 1 <= len(binding.native_id) <= 255
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        base, token, version = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        path = "components/show" if kind == "project" else "issues/search"
        params = (
            {"component": binding.native_id}
            if kind == "project"
            else {
                "componentKeys": binding.native_id,
                "p": page,
                "ps": limit,
            }
        )
        payload, _ = await bounded_read(client, base + "/" + path, params=params, headers={"Authorization": "Bearer " + token})
        result = json.loads(payload)
        if kind == "project":
            if result["component"]["key"] != binding.native_id:
                raise ValueError()
            complete = True
        else:
            paging = result["paging"]
            issues = result["issues"]
            total = paging["total"]
            if (
                type(total) is not int
                or total < 0
                or total > 5 * limit
                or paging["pageIndex"] != page
                or paging["pageSize"] != limit
                or not isinstance(issues, list)
                or len(issues) > limit
                or (page - 1) * limit + len(issues) > total
                or len(issues) != min(limit, max(0, total - (page - 1) * limit))
                or any(
                    not isinstance(issue, dict)
                    or issue.get("project") != binding.native_id
                    or not isinstance(issue.get("key"), str)
                    or not issue["key"]
                    for issue in issues
                )
                or len({issue["key"] for issue in issues}) != len(issues)
            ):
                raise ValueError()
            complete = page * limit >= total
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": complete, "private": False}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def argo_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.expanded_provider import material

    project, separator, namespace = (binding.namespace if isinstance(binding.namespace, str) else "").partition("/")
    if (
        kind not in ("application", "sync_operation")
        or page != 1
        or not project
        or not separator
        or not namespace
        or not isinstance(binding.native_id, str)
        or not 1 <= len(binding.native_id) <= 255
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        base, token, version = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        payload, _ = await bounded_read(
            client,
            base + "/applications/" + quote(binding.native_id, safe=""),
            params={"project": project, "appNamespace": namespace, "refresh": "false"},
            headers={"Authorization": "Bearer " + token},
        )
        data = json.loads(payload)
        if data["metadata"]["name"] != binding.native_id or data["metadata"]["namespace"] != namespace or data["spec"]["project"] != project:
            raise ValueError()
        if kind == "sync_operation":
            history = data["status"].get("history", [])
            current = data["status"].get("operationState")
            if (
                not isinstance(history, list)
                or any(not isinstance(entry, dict) for entry in history)
                or (current is not None and not isinstance(current, dict))
                or len(history) + (current is not None) > limit
            ):
                raise ValueError()
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": True, "private": False}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def slack_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.expanded_provider import material

    if (
        kind not in ("channel", "channel_message")
        or page != 1
        or not isinstance(binding.native_id, str)
        or not re.fullmatch(r"[CG][A-Z0-9]{1,127}", binding.native_id)
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        base, token, version = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        headers = {"Authorization": "Bearer " + token}
        selected, _ = await bounded_read(client, base + "/conversations.info", params={"channel": binding.native_id}, headers=headers)
        identity = json.loads(selected)
        channel = identity["channel"]
        if (
            identity["ok"] is not True
            or channel["id"] != binding.native_id
            or not (channel.get("is_channel") or channel.get("is_group"))
            or channel.get("is_im")
            or channel.get("is_mpim")
            or (channel.get("context_team_id") or "/") != binding.namespace
        ):
            raise ValueError()
        payload = selected
        if kind == "channel_message":
            payload, _ = await bounded_read(
                client,
                base + "/conversations.history",
                params={"channel": binding.native_id, "limit": limit},
                headers=headers,
            )
            history = json.loads(payload)
            messages = history["messages"]
            metadata = history["response_metadata"]
            if (
                history["ok"] is not True
                or not isinstance(messages, list)
                or len(messages) > limit
                or any(not isinstance(message, dict) or not isinstance(message.get("ts"), str) for message in messages)
                or type(history["has_more"]) is not bool
                or history["has_more"]
                or not isinstance(metadata, dict)
                or metadata.get("next_cursor") not in (None, "")
            ):
                raise ValueError()
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": True, "private": False}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def pagerduty_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.expanded_provider import material

    if (
        kind not in ("service", "incident")
        or kind == "service"
        and page != 1
        or binding.namespace != "/"
        or not isinstance(binding.native_id, str)
        or not 1 <= len(binding.native_id) <= 255
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        base, token, version = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        headers = {"Authorization": "Token token=" + token, "Accept": "application/vnd.pagerduty+json;version=2"}
        selected, _ = await bounded_read(client, base + "/services/" + quote(binding.native_id, safe=""), headers=headers)
        service = json.loads(selected)["service"]
        if service["id"] != binding.native_id:
            raise ValueError()
        payload, complete = selected, True
        if kind == "incident":
            offset = (page - 1) * limit
            payload, _ = await bounded_read(
                client,
                base + "/incidents",
                headers=headers,
                params={
                    "service_ids[]": binding.native_id,
                    "date_range": "all",
                    "sort_by": "created_at:desc",
                    "offset": offset,
                    "limit": limit,
                    "total": "true",
                },
            )
            response = json.loads(payload)
            incidents = response["incidents"]
            total = response["total"]
            if (
                type(total) is not int
                or total < 0
                or total > 5 * limit
                or response["offset"] != offset
                or not isinstance(incidents, list)
                or len(incidents) > limit
                or len(incidents) != min(limit, max(0, total - offset))
                or type(response["more"]) is not bool
                or response["more"] != (offset + len(incidents) < total)
                or any(not isinstance(item, dict) or item["service"]["id"] != binding.native_id for item in incidents)
            ):
                raise ValueError()
            complete = not response["more"]
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": complete, "private": False}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def jenkins_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.jenkins import jenkins_credential, job_path, selection

    if (
        kind not in ("job", "build")
        or kind == "job"
        and page != 1
        or not selection(binding.namespace, binding.native_id)
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        endpoint, auth, version = await jenkins_credential(credential, secrets, expected_version=binding.observed["credential_version"])
        target = endpoint + "/" + job_path(binding.native_id) + "/api/json"
        selected, _ = await bounded_read(client, target, auth=auth, params={"tree": "fullName,name,_class"})
        identity = json.loads(selected)
        if (
            len(selected) > 1024 * 1024
            or identity["fullName"] != binding.native_id
            or identity["name"] != binding.native_id.rsplit("/", 1)[-1]
            or identity["_class"] not in ("hudson.model.FreeStyleProject", "org.jenkinsci.plugins.workflow.job.WorkflowJob")
        ):
            raise ValueError()
        payload, complete = selected, True
        if kind == "build":
            offset = (page - 1) * limit
            tree = (
                "fullName,name,_class,allBuilds[timestamp,number,duration,building,estimatedDuration,fullDisplayName,result,"
                "actions[lastBuiltRevision[SHA1,branch[name]],remoteUrls,mercurialRevisionNumber,causes[*]],"
                f"changeSet[kind,revisions[revision]]]{{{offset},{offset + limit}}}"
            )
            payload, _ = await bounded_read(client, target, auth=auth, params={"tree": tree})
            result = json.loads(payload)
            builds = result["allBuilds"]
            if (
                result["fullName"] != binding.native_id
                or result["name"] != identity["name"]
                or result["_class"] != identity["_class"]
                or not isinstance(builds, list)
                or len(builds) > limit
                or any(not isinstance(build, dict) or type(build.get("number")) is not int or build["number"] < 1 for build in builds)
                or len({build["number"] for build in builds}) != len(builds)
                or page == 5
                and len(builds) == limit
            ):
                raise ValueError()
            complete = len(builds) < limit
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": complete, "private": False}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def gitlab_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.expanded_provider import material

    if (
        kind not in ("project", "pipeline", "pipeline_detail")
        or kind == "project"
        and page != 1
        or kind == "pipeline_detail"
        and limit > 10
        or not isinstance(binding.native_id, str)
        or not NATIVE_ID.fullmatch(binding.native_id)
        or not isinstance(binding.namespace, str)
        or not re.fullmatch(r"(?:user:)?[1-9][0-9]{0,19}", binding.namespace)
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        base, token, version = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        headers = {"PRIVATE-TOKEN": token}
        if binding.namespace.startswith("user:"):
            user_bytes, _ = await bounded_read(client, base + "/user", headers=headers)
            if str(json.loads(user_bytes)["id"]) != binding.namespace[5:]:
                raise ValueError()
        selected, _ = await bounded_read(client, base + "/projects/" + binding.native_id, headers=headers)
        project = json.loads(selected)
        namespace = project["namespace"]
        if binding.namespace.startswith("user:"):
            in_scope = namespace["kind"] == "user" and str(project["owner"]["id"]) == binding.namespace[5:]
        else:
            in_scope = namespace["kind"] == "group" and str(namespace["id"]) == binding.namespace
        if str(project["id"]) != binding.native_id or not in_scope or project.get("visibility") not in ("public", "internal", "private"):
            raise ValueError()
        payload, complete = selected, True
        if kind in ("pipeline", "pipeline_detail"):
            payload, paging = await bounded_read(
                client,
                base + "/projects/" + binding.native_id + "/pipelines",
                headers=headers,
                params={"page": page, "per_page": limit, "scope": "all"},
            )
            pipelines = json.loads(payload)
            next_page = paging.get("x-next-page")
            if (
                not isinstance(pipelines, list)
                or len(pipelines) > limit
                or paging.get("x-page") != str(page)
                or paging.get("x-per-page") != str(limit)
                or next_page not in ("", str(page + 1))
                or next_page
                and (page == 5 or len(pipelines) != limit)
                or any(
                    not isinstance(item, dict)
                    or type(item.get("id")) is not int
                    or item["id"] < 1
                    or item.get("project_id") != int(binding.native_id)
                    for item in pipelines
                )
                or len({item["id"] for item in pipelines}) != len(pipelines)
            ):
                raise ValueError()
            complete = not next_page
            if kind == "pipeline_detail":
                if any(
                    len(str(item["id"])) > 19 or not isinstance(item.get("sha"), str) or not isinstance(item.get("status"), str) or not item["status"]
                    for item in pipelines
                ):
                    raise ValueError()
                details = bytearray(b"[")
                for summary in pipelines:
                    native, _ = await bounded_read(
                        client,
                        base + "/projects/" + binding.native_id + "/pipelines/" + str(summary["id"]),
                        headers=headers,
                        params={"with_stats": "true"},
                    )
                    detail = json.loads(native)
                    if (
                        not isinstance(detail, dict)
                        or detail.get("id") != summary["id"]
                        or detail.get("project_id") != int(binding.native_id)
                        or detail.get("sha") != summary.get("sha")
                        or detail.get("status") != summary.get("status")
                        or len(details) + len(native) + 2 > MAX_RESPONSE
                    ):
                        raise ValueError()
                    if len(details) > 1:
                        details.extend(b",")
                    details.extend(native)
                details.extend(b"]")
                payload = bytes(details)
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": complete, "private": project["visibility"] != "public"}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def linear_collection_page(binding, credential, secrets, kind, page, limit, *, client=None):
    from src.product_onboarding.expanded_provider import material

    if (
        kind not in ("team", "issue")
        or kind == "team"
        and page != 1
        or binding.namespace != "/"
        or not isinstance(binding.native_id, str)
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", binding.native_id)
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or credential is None
        or credential.id != binding.credential_id
        or credential.org_id != binding.tenant_id
    ):
        raise HTTPException(409, detail={"code": "source_read_unavailable"})
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        base, token, version = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        headers = {"Authorization": token if credential.credential_type == "api_key" else "Bearer " + token}

        async def query(document):
            async with client.stream("POST", base, json={"query": document}, headers=headers) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESPONSE:
                        raise ValueError()
                    data.extend(chunk)
                return bytes(data), json.loads(data)

        selected_query = "query { team(id: " + json.dumps(binding.native_id) + ") { id name } }"
        _, selected = await query(selected_query)
        if selected.get("errors") or selected["data"]["team"]["id"] != binding.native_id:
            raise ValueError()
        payload, complete = json.dumps(selected, separators=(",", ":")).encode(), True
        if kind == "issue":
            cursor = None
            seen_cursors = set()
            for index in range(1, page + 1):
                after = ", after: " + json.dumps(cursor) if cursor is not None else ""
                document = (
                    "query { team(id: "
                    + json.dumps(binding.native_id)
                    + ") { id issues(first: "
                    + str(limit)
                    + after
                    + ") { nodes { id identifier number title description url priority estimate createdAt updatedAt "
                    "startedAt completedAt canceledAt state { id name type } assignee { id } creator { id } cycle { id } "
                    "parent { id } labels(first: 50) { nodes { id name } } } pageInfo { hasNextPage endCursor } } } }"
                )
                payload, data = await query(document)
                team = data["data"]["team"]
                issues = team["issues"]
                nodes, paging = issues["nodes"], issues["pageInfo"]
                if (
                    data.get("errors")
                    or team["id"] != binding.native_id
                    or not isinstance(nodes, list)
                    or len(nodes) > limit
                    or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in nodes)
                    or len({item["id"] for item in nodes}) != len(nodes)
                    or type(paging["hasNextPage"]) is not bool
                    or paging["hasNextPage"]
                    and (
                        len(nodes) != limit
                        or not isinstance(paging["endCursor"], str)
                        or not 1 <= len(paging["endCursor"]) <= 448
                        or paging["endCursor"] in seen_cursors
                    )
                    or not paging["hasNextPage"]
                    and index != page
                    or paging["hasNextPage"]
                    and index == 5
                ):
                    raise ValueError()
                cursor, complete = paging["endCursor"], not paging["hasNextPage"]
                if not complete:
                    seen_cursors.add(cursor)
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": complete, "private": False}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()


async def provider_page(binding, connection, secrets, kind, page, limit, *, client=None, github_client=None):
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    close_github = False
    try:
        if not 1 <= page <= 5 or not 1 <= limit <= 100:
            raise ValueError()
        if binding.provider == "sonarqube" and isinstance(connection, UserCredential):
            return await sonar_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if binding.provider == "argocd" and isinstance(connection, UserCredential):
            return await argo_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if binding.provider == "slack" and isinstance(connection, UserCredential):
            return await slack_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if binding.provider == "pagerduty" and isinstance(connection, UserCredential):
            return await pagerduty_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if binding.provider == "jenkins" and isinstance(connection, UserCredential):
            return await jenkins_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if binding.provider == "gitlab" and isinstance(connection, UserCredential):
            return await gitlab_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if binding.provider == "linear" and isinstance(connection, UserCredential):
            return await linear_collection_page(binding, connection, secrets, kind, page, limit, client=client)
        if not NATIVE_ID.fullmatch(binding.native_id):
            raise ValueError()
        if binding.provider == "github" and isinstance(connection, ChannelTenantMap):
            from src.admin.connections.github_client import GitHubAppClient
            from src.admin.connections.service import _get_github_app_credentials

            if github_client is None:
                app_id, private_key = _get_github_app_credentials()
                if not app_id or not private_key:
                    raise ValueError()
                github_client = GitHubAppClient(app_id, private_key)
                close_github = True
            repository = await github_client.get_installation_repository(int(connection.installation_id), int(binding.native_id))
            if str(repository.get("id")) != binding.native_id or str((repository.get("owner") or {}).get("id")) != binding.namespace:
                raise ValueError()
            token = await github_client.get_installation_token(int(connection.installation_id))
            if not token:
                raise ValueError()
            suffix = {"repository": "", "issue": "/issues", "pull_request": "/pulls"}.get(kind)
            if suffix is None or kind == "repository" and page != 1:
                raise ValueError()
            payload, headers = await bounded_read(
                client,
                "https://api.github.com/repositories/" + binding.native_id + suffix,
                headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                params={} if kind == "repository" else {"state": "all", "per_page": limit, "page": page},
            )
            complete = kind == "repository" or 'rel="next"' not in headers.get("link", "")
            private = repository.get("private")
            if type(private) is not bool:
                raise ValueError()
            current = await github_client.get_installation_repository(int(connection.installation_id), int(binding.native_id))
            if (
                str(current.get("id")) != binding.native_id
                or str((current.get("owner") or {}).get("id")) != binding.namespace
                or current.get("private") is not private
            ):
                raise ValueError()
        elif binding.provider == "jira" and isinstance(connection, UserCredential):
            site, auth = await jira_auth(connection, secrets)
            if binding.namespace != site or kind not in ("board", "issue") or kind == "board" and page != 1:
                raise ValueError()
            suffix = "/issue" if kind == "issue" else ""
            payload, _ = await bounded_read(
                client,
                site + "/rest/agile/1.0/board/" + binding.native_id + suffix,
                auth=auth,
                params={"startAt": (page - 1) * limit, "maxResults": limit, "fields": "summary,status,created,updated,security,project"}
                if suffix
                else {},
            )
            result = json.loads(payload)
            complete = kind == "board"
            if suffix:
                if (
                    type(result["total"]) is not int
                    or result["total"] < 0
                    or result["startAt"] != (page - 1) * limit
                    or not isinstance(result["issues"], list)
                    or len(result["issues"]) > limit
                    or result["startAt"] + len(result["issues"]) > result["total"]
                ):
                    raise ValueError()
                complete = result["startAt"] + len(result["issues"]) >= result["total"]
                if not complete and len(result["issues"]) != limit:
                    raise ValueError()
            private = False
        else:
            raise ValueError()
        return {"payload": base64.b64encode(payload).decode(), "complete": complete, "private": private}
    except (ValueError, KeyError, TypeError, httpx.HTTPError):
        raise HTTPException(409, detail={"code": "source_read_unavailable"}) from None
    finally:
        if own_client:
            await client.aclose()
        if close_github:
            await github_client.aclose()


@router.get("/{handle}/page")
async def read_page(
    handle: str,
    request: Request,
    response: Response,
    kind: str = Query(
        pattern=r"^(repository|board|project|issue|pull_request|application|sync_operation|channel|channel_message|service|incident|job|build|pipeline|pipeline_detail|team)$"
    ),
    page: int = Query(ge=1, le=5),
    limit: int = Query(ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    secrets=Depends(get_secrets_manager),
):
    authorizer = get_connector_work_authorizer(db)
    before = await read_context(handle, request, response, db, authorizer)
    operation = await db.scalar(
        select(ProductOperation)
        .where(ProductOperation.tenant_id == before["tenant_id"], ProductOperation.id == before["operation_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await db.get(ProductOperation, (before["tenant_id"], operation.request.get("job_id")), populate_existing=True) if operation else None
    if (
        job is None
        or job.state != "running"
        or limit != (min(job.request["page_size"], 10) if before["provider"] == "gitlab" and kind == "pipeline_detail" else job.request["page_size"])
        or operation.request.get("read_count", 0) >= job.request["page_limit"]
    ):
        raise HTTPException(409, detail={"code": "collection_page_bound"})
    operation.request = {**operation.request, "read_count": operation.request.get("read_count", 0) + 1}
    await db.commit()
    binding = await db.get(ProductSourceBinding, (before["tenant_id"], before["binding_id"]), populate_existing=True)
    connection = await db.get(
        ChannelTenantMap if binding.installation_id else UserCredential, binding.installation_id or binding.credential_id, populate_existing=True
    )
    personal_instance = None
    personal_scope = binding.namespace
    operation_id = operation.id
    if binding.provider == "gitlab" and binding.namespace.startswith("user:"):
        from src.internal.profile_discovery_authority import gitlab_personal_identity
        from src.product_onboarding.expanded_provider import material

        if connection is None or connection.user_id != operation.actor_id:
            raise HTTPException(409, detail={"code": "source_read_unavailable"})
        base, _, _ = await material(connection, secrets, expected_version=(binding.observed or {}).get("credential_version"))
        personal_instance = base.removesuffix("/api/v4")
        if not await gitlab_personal_identity(db, before["tenant_id"], operation_id, personal_scope, personal_instance):
            raise HTTPException(409, detail={"code": "source_read_unavailable"})
    result = await provider_page(binding, connection, secrets, kind, page, limit)
    db.expire_all()
    after = await read_context(handle, request, response, db, authorizer)
    if (
        before != after
        or personal_instance is not None
        and not await gitlab_personal_identity(db, before["tenant_id"], operation_id, personal_scope, personal_instance)
    ):
        raise HTTPException(409, detail={"code": "stale_connector_context"})
    response.headers["Cache-Control"] = "no-store"
    return result
