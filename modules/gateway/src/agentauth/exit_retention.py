"""Retain exact worker pods until accepted-abort reporting is durable.

This primitive does not infer exit from deletion or from a finalizer. Callers must
still verify container termination and commit the terminal result before release.
The annotations are discovery hints, never authority: reconciliation must compare
them with the protected execution's tenant, invocation, and workload binding.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

FINALIZER = "adp.aws/abort-terminal-report"
LABEL = "adp.aws/abort-report-pending"
INVOCATION = "adp.aws/abort-invocation"
TENANT = "adp.aws/abort-tenant"
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")


class ExitRetentionError(Exception):
    """Retention was not established or safely released; retry without approval."""


class PodExitRetention:
    def __init__(self, *, client: httpx.Client, namespace: str, service_account: str, token_path: Path):
        if not _NAME.fullmatch(namespace) or not _NAME.fullmatch(service_account):
            raise ValueError("invalid worker namespace or service account")
        self.client = client
        self.namespace = namespace
        self.service_account = service_account
        self.token_path = token_path

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_path.read_text().strip()}"}

    def _read(self, name: str, uid: str) -> tuple[str, dict, dict]:
        if not _NAME.fullmatch(name) or not uid:
            raise ExitRetentionError("invalid pod identity")
        path = f"/api/v1/namespaces/{self.namespace}/pods/{name}"
        response = self.client.get(path, headers=self._headers())
        response.raise_for_status()
        pod = response.json()
        metadata = pod["metadata"]
        if metadata.get("uid") != uid or not metadata.get("resourceVersion") or pod.get("spec", {}).get("serviceAccountName") != self.service_account:
            raise ExitRetentionError("worker identity changed")
        return path, pod, metadata

    def _patch(self, path: str, metadata: dict, changes: list[dict]) -> None:
        # Test both fields at the actual mutation boundary. A read followed by an
        # unconditional patch could modify a replacement or lose another finalizer.
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            *changes,
        ]
        response = self.client.patch(path, headers={**self._headers(), "Content-Type": "application/json-patch+json"}, json=patch)
        response.raise_for_status()

    def discover(self, *, cursor: str = "") -> tuple[list[dict], str]:
        """Return one bounded page of hints, including terminating pods.

        Discovery is independent of SQL work claims. An accepted abort must remain
        recoverable even when its run never acquired a claim or already released it.
        The consumer must authenticate every hint against the protected EXEC row.
        """
        try:
            response = self.client.get(
                f"/api/v1/namespaces/{self.namespace}/pods",
                headers=self._headers(),
                params={"labelSelector": f"{LABEL}=true", "limit": "100", "continue": cursor},
            )
            response.raise_for_status()
            page = response.json()
            hints = []
            for pod in page["items"]:
                metadata = pod["metadata"]
                annotations = metadata.get("annotations") or {}
                if (
                    pod.get("spec", {}).get("serviceAccountName") == self.service_account
                    and FINALIZER in (metadata.get("finalizers") or [])
                    and all(
                        isinstance(value, str) and value
                        for value in (
                            metadata.get("name"),
                            metadata.get("uid"),
                            annotations.get(INVOCATION),
                            annotations.get(TENANT),
                        )
                    )
                ):
                    hints.append(
                        {"name": metadata["name"], "uid": metadata["uid"], "invocation_id": annotations[INVOCATION], "tenant_id": annotations[TENANT]}
                    )
            cursor = page.get("metadata", {}).get("continue", "")
            if not isinstance(cursor, str):
                raise ValueError("invalid page cursor")
            return hints, cursor
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            raise ExitRetentionError("could not discover retained worker pods") from exc

    def retain(self, *, name: str, uid: str, invocation_id: str, tenant_id: str) -> None:
        if not invocation_id or not tenant_id:
            raise ExitRetentionError("missing protected run identity")
        try:
            path, _, metadata = self._read(name, uid)
            finalizers = list(metadata.get("finalizers") or [])
            annotations = dict(metadata.get("annotations") or {})
            labels = dict(metadata.get("labels") or {})
            binding = {INVOCATION: invocation_id, TENANT: tenant_id}
            if any(key in annotations and annotations[key] != value for key, value in binding.items()):
                raise ExitRetentionError("pod retention belongs to another run")
            if FINALIZER in finalizers:
                if all(annotations.get(key) == value for key, value in binding.items()) and labels.get(LABEL) == "true":
                    return
                raise ExitRetentionError("incomplete existing retention")
            if metadata.get("deletionTimestamp"):
                raise ExitRetentionError("pod deletion already started")
            finalizers.append(FINALIZER)
            annotations.update(binding)
            labels[LABEL] = "true"
            self._patch(
                path,
                metadata,
                [
                    {"op": "add", "path": "/metadata/finalizers", "value": finalizers},
                    {"op": "add", "path": "/metadata/annotations", "value": annotations},
                    {"op": "add", "path": "/metadata/labels", "value": labels},
                ],
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            raise ExitRetentionError("could not retain worker exit evidence") from exc

    def release(self, *, name: str, uid: str, invocation_id: str, tenant_id: str) -> None:
        """Remove only this finalizer after the caller durably reports the outcome."""
        try:
            path, _, metadata = self._read(name, uid)
            finalizers = list(metadata.get("finalizers") or [])
            if FINALIZER not in finalizers:
                return
            annotations = dict(metadata.get("annotations") or {})
            if annotations.get(INVOCATION) != invocation_id or annotations.get(TENANT) != tenant_id:
                raise ExitRetentionError("pod retention belongs to another run")
            labels = dict(metadata.get("labels") or {})
            finalizers.remove(FINALIZER)
            annotations.pop(INVOCATION)
            annotations.pop(TENANT)
            labels.pop(LABEL, None)
            self._patch(
                path,
                metadata,
                [
                    {"op": "add", "path": "/metadata/finalizers", "value": finalizers},
                    {"op": "add", "path": "/metadata/annotations", "value": annotations},
                    {"op": "add", "path": "/metadata/labels", "value": labels},
                ],
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            raise ExitRetentionError("could not release worker exit evidence") from exc
