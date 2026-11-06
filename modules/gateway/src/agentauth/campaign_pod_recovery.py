"""Exact-pod shutdown retains positive exit evidence independently of worker auth."""

import httpx

from src.agentauth.exit_retention import ExitRetentionError, PodExitRetention

FINALIZER = "adp.aws/campaign-exit"
INVOCATION = "adp.aws/campaign-invocation"
TENANT = "adp.aws/campaign-tenant"
OPERATION = "adp.aws/campaign-operation"


class CampaignPodRecovery(PodExitRetention):
    def retain(self, *, name, uid, invocation_id, tenant_id, operation_id):
        binding = {INVOCATION: invocation_id, TENANT: tenant_id, OPERATION: operation_id}
        if not all(isinstance(value, str) and value for value in binding.values()):
            raise ExitRetentionError("campaign retention requires protected ownership")
        try:
            path, _, metadata = self._read(name, uid)
            annotations = dict(metadata.get("annotations") or {})
            finalizers = list(metadata.get("finalizers") or [])
            if any(key in annotations and annotations[key] != value for key, value in binding.items()):
                raise ExitRetentionError("campaign retention belongs to another operation")
            if FINALIZER in finalizers:
                if any(annotations.get(key) != value for key, value in binding.items()):
                    raise ExitRetentionError("campaign retention is incomplete")
                return
            if metadata.get("deletionTimestamp"):
                raise ExitRetentionError("workload deletion began before campaign retention")
            annotations.update(binding)
            self._patch(
                path,
                metadata,
                [
                    {"op": "add", "path": "/metadata/annotations", "value": annotations},
                    {"op": "add", "path": "/metadata/finalizers", "value": [*finalizers, FINALIZER]},
                ],
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
            raise ExitRetentionError("campaign exit retention unavailable") from error

    def _retained(self, name, uid, invocation_id, tenant_id, operation_id):
        path, pod, metadata = self._read(name, uid)
        binding = {INVOCATION: invocation_id, TENANT: tenant_id, OPERATION: operation_id}
        if FINALIZER not in (metadata.get("finalizers") or []) or any(
            metadata.get("annotations", {}).get(key) != value for key, value in binding.items()
        ):
            raise ExitRetentionError("campaign retention ownership changed")
        return path, pod, metadata

    def retained(self, *, name, uid, invocation_id, tenant_id, operation_id):
        try:
            _, _, metadata = self._read(name, uid)
            if FINALIZER not in (metadata.get("finalizers") or []):
                return False
            self._retained(name, uid, invocation_id, tenant_id, operation_id)
            return True
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
            raise ExitRetentionError("campaign retention observation unavailable") from error

    def request_stop(self, *, name, uid, invocation_id, tenant_id, operation_id):
        try:
            path, _, metadata = self._retained(name, uid, invocation_id, tenant_id, operation_id)
            if metadata.get("deletionTimestamp"):
                return
            response = self.client.request(
                "DELETE",
                path,
                headers=self._headers(),
                json={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "gracePeriodSeconds": 30,
                    "propagationPolicy": "Background",
                    "preconditions": {"uid": uid, "resourceVersion": metadata["resourceVersion"]},
                },
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
            raise ExitRetentionError("campaign stop outcome unavailable; retain ownership and retry") from error

    def release(self, *, name, uid, invocation_id, tenant_id, operation_id):
        try:
            path, _, metadata = self._read(name, uid)
            finalizers = list(metadata.get("finalizers") or [])
            if FINALIZER not in finalizers:
                return
            self._retained(name, uid, invocation_id, tenant_id, operation_id)
            annotations = dict(metadata.get("annotations") or {})
            for key in (INVOCATION, TENANT, OPERATION):
                annotations.pop(key, None)
            finalizers.remove(FINALIZER)
            self._patch(
                path,
                metadata,
                [
                    {"op": "add", "path": "/metadata/annotations", "value": annotations},
                    {"op": "add", "path": "/metadata/finalizers", "value": finalizers},
                ],
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                try:
                    status = error.response.json()
                    if (
                        status.get("kind") == "Status"
                        and status.get("reason") == "NotFound"
                        and status.get("details", {}).get("name") == name
                        and status.get("details", {}).get("kind") == "pods"
                    ):
                        return
                except (ValueError, AttributeError, TypeError):
                    pass
            raise ExitRetentionError("campaign retention release unavailable") from error
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
            raise ExitRetentionError("campaign retention release unavailable") from error
