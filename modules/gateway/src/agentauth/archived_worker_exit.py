"""Read a bounded, provider-owned terminal Pod observation; never trust uploaded evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError


class ExitRecoveryRefusedError(Exception):
    """The requested recovery is not established by protected identity and exit evidence."""


class ExitRecoveryUnavailableError(Exception):
    """The observation or transaction could not be established; retain the same operation ID."""


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ArchivedWorkerExit:
    audit_id: str
    event_id: str
    digest: str
    image_digest: str
    finished_at: str
    exit_code: int


class ArchivedWorkerExitReader:
    def __init__(self, *, client, log_group: str, namespace: str, service_account: str, image_digests: frozenset[str]):
        if not re.fullmatch(r"/aws/eks/[A-Za-z0-9][A-Za-z0-9_-]{0,99}/cluster", log_group) or not image_digests:
            raise ExitRecoveryUnavailableError("worker audit source unavailable")
        self.client, self.log_group = client, log_group
        self.namespace, self.service_account, self.image_digests = namespace, service_account, image_digests

    def observe(self, *, audit_id: str, audit_at: str, name: str, uid: str, arrived_at: str) -> ArchivedWorkerExit:
        if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", audit_id):
            raise ExitRecoveryRefusedError("invalid audit identity")
        try:
            hint, arrived = instant(audit_at), instant(arrived_at)
            if not arrived <= hint <= datetime.now(UTC) + timedelta(seconds=30):
                raise ValueError()
            timestamp = int(hint.timestamp() * 1000)
            arguments = dict(
                logGroupName=self.log_group,
                startTime=timestamp - 60_000,
                endTime=timestamp + 60_001,
                filterPattern='{ $.auditID = "' + audit_id + '" }',
                limit=20,
            )
            found = {}
            for _ in range(4):
                page = self.client.filter_log_events(**arguments)
                for event in page.get("events", []):
                    message = event.get("message", "")
                    if not isinstance(message, str) or len(message.encode()) > 1_048_576:
                        raise ValueError()
                    item = json.loads(message)
                    if isinstance(item, dict) and item.get("auditID") == audit_id and item.get("stage") == "ResponseComplete":
                        found[event["eventId"]] = (item, event, message)
                token = page.get("nextToken")
                if not token:
                    break
                if token == arguments.get("nextToken"):
                    raise ExitRecoveryUnavailableError("worker audit pagination incomplete")
                arguments["nextToken"] = token
            else:
                raise ExitRecoveryUnavailableError("worker audit pagination incomplete")
            if len(found) != 1:
                raise ValueError()
            audit, event, message = next(iter(found.values()))
            pod = audit["responseObject"]
            metadata, spec, status = pod["metadata"], pod["spec"], pod["status"]
            workers = [entry for entry in status["containerStatuses"] if entry.get("name") == "agent-worker"]
            containers = [entry for entry in spec["containers"] if entry.get("name") == "agent-worker"]
            if len(workers) != 1 or len(containers) != 1:
                raise ValueError()
            worker = workers[0]
            terminal = worker["state"]["terminated"]
            image = worker["imageID"].rsplit("@", 1)[-1]
            observed, started, finished = instant(audit["stageTimestamp"]), instant(terminal["startedAt"]), instant(terminal["finishedAt"])
            if not (
                audit.get("kind") == "Event"
                and audit.get("apiVersion") == "audit.k8s.io/v1"
                and audit.get("level") == "RequestResponse"
                and audit.get("stage") == "ResponseComplete"
                and audit.get("verb") == "delete"
                and audit.get("responseStatus", {}).get("code") == 200
                and audit.get("requestURI") == f"/api/v1/namespaces/{self.namespace}/pods/{name}"
                and audit.get("objectRef", {}).get("resource") == "pods"
                and audit.get("objectRef", {}).get("name") == name
                and audit.get("objectRef", {}).get("namespace") == self.namespace
                and pod.get("kind") == "Pod"
                and pod.get("apiVersion") == "v1"
                and metadata.get("name") == name
                and metadata.get("uid") == uid
                and metadata.get("namespace") == self.namespace
                and spec.get("serviceAccountName") == self.service_account
                and spec.get("restartPolicy") == "Never"
                and status.get("phase") in {"Succeeded", "Failed"}
                and type(terminal.get("exitCode")) is int
                and image in self.image_digests
                and containers[0].get("image", "").endswith("@" + image)
                and arrived <= started <= finished <= observed
                and abs((observed - hint).total_seconds()) <= 60
                and isinstance(event.get("eventId"), str)
                and event["eventId"]
            ):
                raise ValueError()
            return ArchivedWorkerExit(
                audit_id, event["eventId"], hashlib.sha256(message.encode()).hexdigest(), image, terminal["finishedAt"], terminal["exitCode"]
            )
        except (ClientError, BotoCoreError):
            raise ExitRecoveryUnavailableError("worker audit source unavailable") from None
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise ExitRecoveryRefusedError("verified worker exit unavailable") from None
