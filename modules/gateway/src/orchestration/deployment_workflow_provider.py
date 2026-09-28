"""Bounded GitHub workflow reads and scoped, correlated dispatch for ENGINE-D2."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

from .review_cycle import CycleBlockedError

CONTEXT_ARTIFACT = "adp-deployment-context"
CORRELATION_INPUT = "adp_correlation"
SOURCE_INPUT = "adp_source_revision"
DEFINITION_INPUT = "adp_definition_revision"
TRANSPORT_INPUTS = frozenset({CORRELATION_INPUT, SOURCE_INPUT, DEFINITION_INPUT})
MAX_CONTEXT_BYTES = 65536
MAX_ARCHIVE_BYTES = 1024 * 1024


class WorkflowContext(BaseModel):
    """Provider-owned context artifact, authenticated by its run and definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(ge=1, le=1)
    repository_id: int = Field(gt=0)
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    workflow_path: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
    workflow_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(min_length=1, max_length=64)
    resource_kind: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=512)
    inputs: dict[str, str] = Field(max_length=20)
    correlation: str = Field(max_length=64)


@dataclass(frozen=True)
class WorkflowDefinition:
    approved_revision: str
    source_revision: str
    blob_sha: str
    defaults: dict[str, str]
    dispatchable: bool
    dispatch_ref: str
    dispatch_revision: str
    caller_path: str | None = None
    caller_blob_sha: str | None = None


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    run_attempt: int
    status: str
    conclusion: str | None
    url: str
    context: WorkflowContext
    artifact_id: int
    artifact_digest: str
    observed_at: str


class WorkflowProvider:
    def __init__(self, *, client=None, clock=lambda: datetime.now(UTC)):
        self.client, self.clock = client, clock

    async def token(self, binding, *, write=False):
        app, key = await resolve_tenant_app_credentials(binding.org_id)
        token, expires = await mint_installation_token_with_expiry(
            app,
            key,
            binding.installation_id,
            repositories=[binding.repo.split("/", 1)[1]],
            permissions={"actions": "write" if write else "read", "contents": "read", "metadata": "read"},
        )
        deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline <= self.clock():
            raise CycleBlockedError("deployment_scoped_credential_expired")
        return token

    async def request(self, binding, method, path, *, token=None, max_bytes=MAX_ARCHIVE_BYTES, **kwargs):
        token = token or await self.token(binding)
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False)
        try:
            async with client.stream(
                method,
                "https://api.github.com" + path,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                **kwargs,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise CycleBlockedError("deployment_provider_response_limit")
                    body.extend(chunk)
                return httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)

        finally:
            if owned:
                await client.aclose()

    async def pages(self, binding, path, key=None, **params):
        values = []
        token = await self.token(binding)
        for page in range(1, 11):
            response = await self.request(binding, "GET", path, token=token, params={**params, "page": page, "per_page": 100})
            data = response.json()
            items = data.get(key) if key else data
            if not isinstance(items, list):
                raise CycleBlockedError("deployment_provider_response_invalid")
            values.extend(items)
            if len(items) < 100:
                return values
        raise CycleBlockedError("deployment_provider_history_limit")

    async def changed_files(self, binding):
        rows = await self.pages(binding, f"/repos/{binding.repo}/pulls/{binding.pr_number}/files")
        paths = []
        for row in rows:
            paths.append(row["filename"])
            if row.get("previous_filename"):
                paths.append(row["previous_filename"])
        if not paths or any(not isinstance(path, str) or len(path) > 1024 or path.startswith("/") or ".." in path.split("/") for path in paths):
            raise CycleBlockedError("deployment_changed_paths_unverifiable")
        return tuple(sorted(set(paths)))

    async def definition(self, binding, workflow, source_revision, *, for_dispatch=True):
        import yaml

        repository = (await self.request(binding, "GET", f"/repos/{binding.repo}")).json()
        if repository.get("id") != binding.provider_repository_id:
            raise CycleBlockedError("deployment_repository_identity_mismatch")
        dispatch_ref, dispatch_revision = "", source_revision
        if for_dispatch:
            dispatch_ref = repository.get("default_branch")
            if not isinstance(dispatch_ref, str) or not dispatch_ref:
                raise CycleBlockedError("deployment_dispatch_ref_missing")
            branch = (await self.request(binding, "GET", f"/repos/{binding.repo}/branches/{quote(dispatch_ref, safe='')}")).json()
            dispatch_revision = branch.get("commit", {}).get("sha")
            if not isinstance(dispatch_revision, str) or len(dispatch_revision) != 40:
                raise CycleBlockedError("deployment_dispatch_revision_missing")
        blobs = []
        for revision in (workflow.definition_revision, source_revision, dispatch_revision):
            response = await self.request(binding, "GET", f"/repos/{binding.repo}/contents/{workflow.path}", params={"ref": revision})
            record = response.json()
            if record.get("type") != "file" or record.get("encoding") != "base64" or record.get("size", MAX_CONTEXT_BYTES + 1) > MAX_CONTEXT_BYTES:
                raise CycleBlockedError("deployment_workflow_definition_unverifiable")
            content = base64.b64decode(record["content"])
            # Git's blob object ID, not a security digest: SHA-1 over "blob <len>\0<bytes>" IS
            # the name GitHub reports in `sha`, so the algorithm is fixed by Git's object format
            # and a stronger hash would match nothing. Authorization comes from the approved
            # revision pin, the allowed-input check and the scoped token, never from this value;
            # cross-revision equality below compares the content bytes, not just this digest.
            blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
            if blob != record.get("sha"):
                raise CycleBlockedError("deployment_workflow_blob_mismatch")
            blobs.append((blob, content))
        if any(blob != blobs[0] for blob in blobs[1:]):
            raise CycleBlockedError("deployment_workflow_revision_mismatch")
        doc = yaml.safe_load(blobs[0][1])
        if not isinstance(doc, dict):
            raise CycleBlockedError("deployment_workflow_definition_invalid")
        events = doc.get("on", doc.get(True, {}))
        dispatch = events.get("workflow_dispatch") if isinstance(events, dict) else None
        dispatchable = isinstance(events, dict) and "workflow_dispatch" in events
        fields = (dispatch or {}).get("inputs", {}) if isinstance(dispatch, dict) or dispatch is None else {}
        defaults = {name: str((field or {}).get("default", "")) for name, field in fields.items()}
        caller_path, caller_blob = None, None
        if workflow.path == ".github/workflows/run-gateway-migrations.yml":
            # A reusable workflow's artifact shares the caller run. Pin the one
            # supported caller too, so another workflow cannot forge its context.
            from dataclasses import replace

            caller_path = ".github/workflows/gateway-deploy.yml"
            caller = await self.definition(binding, replace(workflow, path=caller_path), source_revision, for_dispatch=for_dispatch)
            caller_blob = caller.blob_sha
        return WorkflowDefinition(
            workflow.definition_revision,
            source_revision,
            blobs[0][0],
            defaults,
            dispatchable,
            dispatch_ref,
            dispatch_revision,
            caller_path,
            caller_blob,
        )

    async def runs(self, binding, source_revision, correlation=None):
        params = {} if correlation else {"head_sha": source_revision}
        return await self.pages(binding, f"/repos/{binding.repo}/actions/runs", "workflow_runs", **params)

    async def context(self, binding, run, workflow_path):
        """Only download an artifact through the API for the verified bound run."""
        run_id, attempt = int(run["id"]), int(run["run_attempt"])
        name = f"{CONTEXT_ARTIFACT}-{workflow_path.rsplit('/', 1)[1]}-{attempt}"
        rows = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run_id}/artifacts", "artifacts")
        matches = [row for row in rows if row.get("name") == name and not row.get("expired")]
        if not matches:
            return None
        if len(matches) != 1:
            raise CycleBlockedError("deployment_context_ambiguous")
        artifact = matches[0]
        if not 0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE_BYTES:
            raise CycleBlockedError("deployment_context_size_invalid")
        archive = await self.request(binding, "GET", f"/repos/{binding.repo}/actions/artifacts/{int(artifact['id'])}/zip", follow_redirects=True)
        payload = archive.content
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise CycleBlockedError("deployment_context_size_invalid")
        digest = hashlib.sha256(payload).hexdigest()
        if artifact.get("digest") != "sha256:" + digest:
            raise CycleBlockedError("deployment_context_digest_mismatch")
        with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
            entries = zipped.infolist()
            if len(entries) != 1 or entries[0].filename != "deployment-context.json" or entries[0].file_size > MAX_CONTEXT_BYTES:
                raise CycleBlockedError("deployment_context_archive_invalid")
            context = WorkflowContext.model_validate_json(zipped.read(entries[0]))
        if (context.repository_id, context.run_id, context.run_attempt, context.workflow_path) != (
            binding.provider_repository_id,
            run_id,
            attempt,
            workflow_path,
        ):
            raise CycleBlockedError("deployment_context_identity_mismatch")
        return context, int(artifact["id"]), digest

    async def observe(self, binding, *, workflow, definition, target, source_revision, inputs, correlation=None, run_id=None):
        runs = (
            [(await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run_id}")).json()]
            if run_id
            else await self.runs(binding, source_revision, correlation)
        )
        matches, incomplete, mismatch = [], False, None
        for run in runs:
            expected_workflow_revision = definition.dispatch_revision if run.get("event") == "workflow_dispatch" else source_revision
            if run.get("head_sha") != expected_workflow_revision or (run.get("repository") or {}).get("id") != binding.provider_repository_id:
                if run_id:
                    raise CycleBlockedError("deployment_run_identity_mismatch")
                continue
            parent_path = str(run.get("path", "")).split("@", 1)[0]
            if parent_path != workflow.path:
                references = run.get("referenced_workflows", [])
                if (
                    parent_path != definition.caller_path
                    or not definition.caller_blob_sha
                    or not any(
                        str(ref.get("path", "")).split("@", 1)[0].removeprefix(binding.repo + "/") == workflow.path
                        and ref.get("sha") == expected_workflow_revision
                        for ref in references
                    )
                ):
                    continue
            if run.get("event") not in {"push", "workflow_dispatch"}:
                continue
            if run.get("event") == "workflow_dispatch" and (not correlation or run.get("display_title") != "ADP deployment " + correlation):
                continue
            result = await self.context(binding, run, workflow.path)
            if result is None:
                incomplete = True
                continue
            context, artifact_id, digest = result
            expected = (expected_workflow_revision, source_revision, target.account_id, target.region, target.resource_kind, target.resource_id)
            actual = (
                context.workflow_revision,
                context.source_revision,
                context.account_id,
                context.region,
                context.resource_kind,
                context.resource_id,
            )
            if actual != expected:
                mismatch = "deployment_run_target_or_revision_mismatch"
                if run_id:
                    raise CycleBlockedError(mismatch)
                continue
            actual_inputs = {name: value for name, value in context.inputs.items() if name not in TRANSPORT_INPUTS}
            effective_inputs = {**definition.defaults, **inputs}
            for name in TRANSPORT_INPUTS:
                effective_inputs.pop(name, None)
            if run.get("event") == "push":
                # Automatic runs can use approved defaults rather than the
                # registered-connection inputs needed for manual dispatch.
                valid_inputs = actual_inputs.keys() == effective_inputs.keys() and all(
                    value in workflow.allowed_inputs[name] if workflow.allowed_inputs.get(name) else value == definition.defaults.get(name)
                    for name, value in actual_inputs.items()
                )
            else:
                valid_inputs = actual_inputs == effective_inputs
            if not valid_inputs:
                mismatch = "deployment_run_inputs_mismatch"
                if run_id:
                    raise CycleBlockedError(mismatch)
                continue
            if run.get("event") == "workflow_dispatch" and (not correlation or context.correlation != correlation):
                raise CycleBlockedError("deployment_dispatch_correlation_mismatch")
            status = run.get("status")
            if status not in {"queued", "in_progress", "completed", "waiting", "pending", "requested"}:
                raise CycleBlockedError("deployment_run_status_unknown")
            matches.append(
                WorkflowRun(
                    int(run["id"]),
                    int(run["run_attempt"]),
                    status,
                    run.get("conclusion"),
                    f"https://github.com/{binding.repo}/actions/runs/{int(run['id'])}",
                    context,
                    artifact_id,
                    digest,
                    self.clock().isoformat(),
                )
            )
        if len(matches) > 1:
            raise CycleBlockedError("deployment_run_ambiguous")
        if not matches and mismatch:
            raise CycleBlockedError(mismatch)
        return (matches[0] if matches else None), incomplete

    async def dispatch(self, binding, *, workflow, definition, source_revision, inputs, correlation, reauthorize):
        if not definition.dispatchable or workflow.correlation_input != CORRELATION_INPUT or not TRANSPORT_INPUTS.issubset(definition.defaults):
            raise CycleBlockedError("deployment_workflow_correlation_unavailable")
        if workflow.check_inputs(inputs):
            raise CycleBlockedError("deployment_input_not_permitted")
        token = await self.token(binding, write=True)
        await reauthorize()
        await self.request(
            binding,
            "POST",
            f"/repos/{binding.repo}/actions/workflows/{quote(workflow.path.rsplit('/', 1)[1])}/dispatches",
            token=token,
            json={
                "ref": definition.dispatch_ref,
                "inputs": {**inputs, CORRELATION_INPUT: correlation, SOURCE_INPUT: source_revision, DEFINITION_INPUT: definition.dispatch_revision},
            },
        )
