"""Thin Task-host adapter for the existing native AgentCore browser integration.

Runs in the trusted worker, outside the credential-free Claude SDK process.
The maintained local_browser module owns Playwright, sessions and expiry.
"""

import base64
import hashlib
import io
import json
import secrets
import threading

from fastapi import HTTPException
from adp_tools.authority import TaskHostAuthority
from adp_tools.evidence import put_blob
from agentcore_tools.url_contract import checked_url, validate_url_payload


class TaskBrowser:
    def __init__(self, client, request=None):
        self.authority = TaskHostAuthority(
            lambda action, body: client._post(action, body, run_bound=True)
        )
        if request is None:
            from agentcore_tools.browser_runtime.local_browser import investigation_request

            request = investigation_request
        self.request = request
        self.sessions, self.receipts, self.ids = {}, {}, {}
        self.lock = threading.RLock()
        self.closed = False

    def invoke(self, body):
        with self.lock:
            base = {
                "schema_version": "1.0",
                "task_id": body["attempt"]["run"]["task_id"],
                "operation_id": body["operation_id"],
            }
            operation, payload = body["operation"], body["payload"]
            cleanup = operation == "cancel_jobs"
            verified = self.authority.authorize(
                attempt=body["attempt"], tool="cyber." + operation, cleanup=cleanup
            )
            identity = verified.identity
            if cleanup:
                self.closed = True
                pending = []
                for sid, session in self.sessions.items():
                    if session["owner"] != identity.model_dump():
                        continue
                    if session.get("cleanup") == "stopped":
                        continue
                    try:
                        packet = self.request(
                            "close", {"session_token": session["token"]}
                        )
                        session["cleanup"] = packet.get("cleanup_status", "unknown")
                    except Exception:
                        session["cleanup"] = "unknown"
                    if session["cleanup"] != "stopped":
                        pending.append(sid)
                # An uncertain start with no returned token cannot be declared stopped.
                pending += [
                    key for key, receipt in self.receipts.items() if receipt is None
                ]
                return {
                    **base,
                    "operation_status": "pending" if pending else "confirmed",
                    "result": {
                        "status": "pending" if pending else "confirmed",
                        "pending_jobs": pending,
                    },
                }
            try:
                validate_url_payload(operation, payload)
                if self.closed:
                    raise HTTPException(409, "Browser tools closed for this attempt")
                owner = identity.model_dump()
                digest = hashlib.sha256(
                    json.dumps([owner, operation, payload], sort_keys=True).encode()
                ).hexdigest()
                previous = self.ids.get(body["operation_id"])
                if previous and previous != digest:
                    raise HTTPException(409, "Tool operation ID reused")
                if len(self.ids) >= 128 and not previous:
                    raise HTTPException(429, "Browser operation bound exceeded")
                self.ids[body["operation_id"]] = digest
                if digest in self.receipts:
                    return {
                        **(
                            self.receipts[digest]
                            or {
                                "operation_status": "unknown",
                                "result": {"status": "unknown"},
                            }
                        ),
                        **base,
                    }
                if operation == "browser_start":
                    inputs = verified.task.get("input_payload", {}).get("inputs", {})
                    if payload["url"] not in [
                        inputs.get("url"),
                        *inputs.get("urls", []),
                    ]:
                        raise HTTPException(403, "URL was not supplied to this Task")
                    checked_url(payload["url"])
                    if (
                        inputs.get("browser_scope") == "host"
                        and payload.get("scope", "observed_external") != "host"
                    ):
                        raise HTTPException(403, "Task restricts browser scope to host")
                    self.receipts[digest] = None
                    packet = self.request(
                        "start",
                        {k: v for k, v in payload.items() if k != "session_key"},
                    )
                    sid = secrets.token_hex(32)
                    session = {
                        "token": packet.pop("session_token"),
                        "owner": owner,
                        "packet": {},
                        "cleanup": "open",
                    }
                    self.sessions[sid] = session
                else:
                    sid = payload["session_id"]
                    session = self.sessions.get(sid)
                    if not session or session["owner"] != owner:
                        raise HTTPException(404, "Browser session unavailable")
                    if operation == "browser_inspect":
                        result = self.inspect(session, payload)
                        return self.receipt(base, identity, result)
                    if operation not in {"browser_step", "browser_close"}:
                        raise HTTPException(422, "Unsupported browser operation")
                    if operation == "browser_step" and payload["action"] == "navigate":
                        checked_url(payload["url"])
                    self.receipts[digest] = None
                    request = {k: v for k, v in payload.items() if k != "session_id"}
                    request["session_token"] = session["token"]
                    packet = self.request(
                        "close" if operation == "browser_close" else "step", request
                    )
                session["packet"] = packet
                session["cleanup"] = packet.get(
                    "cleanup_status",
                    packet.get("manifest", {}).get(
                        "cleanup_status", session["cleanup"]
                    ),
                )
                if (
                    self.authority.authorize(
                        attempt=body["attempt"], tool="cyber." + operation
                    ).identity
                    != identity
                ):
                    raise HTTPException(403, "Task authority changed")
                artifacts = self.capture(identity, packet)
                result = {
                    "status": "completed",
                    "session_id": sid,
                    "view_id": packet.get("view_id", ""),
                    "session_open": packet.get("session_open", False),
                    "cleanup_status": session["cleanup"],
                    "choices": packet.get("choices", [])[:12],
                    "evidence_artifacts": artifacts,
                    "observations": [
                        {
                            k: v[:2000] if isinstance(v, str) else v
                            for k, v in o.items()
                            if k
                            in {
                                "id",
                                "page_title",
                                "visible_text",
                                "final_url",
                                "http_status",
                                "errors",
                            }
                        }
                        for o in packet.get("observations", [])[:2]
                    ],
                }
                receipt = self.receipt(base, identity, result)
                self.receipts[digest] = receipt
                return receipt
            except HTTPException as error:
                return {
                    **base,
                    "operation_status": "rejected",
                    "result": {"status": "refused", "reason": error.detail},
                }
            except Exception:
                # Never replay a failed browser action. Host cleanup still closes owned sessions.
                if "digest" in locals() and "session" in locals():
                    self.receipts[digest] = {
                        **base,
                        "operation_status": "unknown",
                        "result": {"status": "unknown"},
                    }
                return {
                    **base,
                    "operation_status": "unknown",
                    "result": {
                        "status": "unknown",
                        "reason": "browser_outcome_unavailable",
                    },
                }

    def receipt(self, base, identity, result):
        content = json.dumps(result, sort_keys=True).encode()
        if len(content) > 24000:
            raise ValueError("Browser receipt exceeds bound")
        artifact = self.authority.put_run_artifact(
            attempt=identity,
            content=content,
            content_type="application/json",
            digest=hashlib.sha256(content).hexdigest(),
        )
        return {
            **base,
            "operation_status": "confirmed",
            "result": result,
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "content_type": artifact.content_type,
                "content_sha256": artifact.content_sha256,
                "byte_length": len(content),
            },
        }

    def capture(self, identity, packet):
        copied = json.loads(json.dumps(packet))
        for observation in copied.get("observations", []):
            for field, mime in [
                ("screenshot_base64", "image/png"),
                ("dom_snapshot", "text/html"),
            ]:
                value = observation.pop(field, None)
                if value:
                    raw = (
                        base64.b64decode(value, validate=True)
                        if field == "screenshot_base64"
                        else value.encode()
                    )
                    observation[field + "_artifact"] = put_blob(
                        self.authority, identity, raw, mime
                    )
        raw = json.dumps(copied, sort_keys=True).encode()
        artifact = self.authority.put_run_artifact(
            attempt=identity,
            content=raw,
            content_type="application/json",
            digest=hashlib.sha256(raw).hexdigest(),
        )
        return [artifact.artifact_id]

    def inspect(self, session, payload):
        section = payload.get("section", "summary")
        packet = session["packet"]
        observations = packet.get("observations", [])
        if section == "screenshot":
            from PIL import Image

            encoded = next(
                (
                    o.get("screenshot_base64")
                    for o in reversed(observations)
                    if o.get("screenshot_base64")
                ),
                None,
            )
            if not encoded:
                return {
                    "status": "completed",
                    "reason": "No screenshot in current view",
                }
            picture = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
            picture.thumbnail((640, 640))
            output = io.BytesIO()
            for quality in (65, 45, 25, 10):
                output.seek(0)
                output.truncate()
                picture.save(output, format="JPEG", quality=quality)
                if len(output.getvalue()) <= 12000:
                    break
            if len(output.getvalue()) > 12000:
                return {
                    "status": "completed",
                    "reason": "Screenshot preview exceeds model bound; original retained as artifact",
                }
            return {
                "status": "completed",
                "image": {
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(output.getvalue()).decode(),
                },
            }
        if section == "summary":
            # Binary evidence is available via screenshot/artifacts, not text paging.
            value = {
                **packet,
                "observations": [
                    {
                        key: item
                        for key, item in observation.items()
                        if key not in {"screenshot_base64", "dom_snapshot"}
                    }
                    for observation in observations
                ],
            }
        elif section == "choices":
            value = packet.get("choices", [])
        else:
            field = {"dom": "dom_snapshot", "network": "network_requests"}.get(
                section, section
            )
            value = [observation.get(field) for observation in observations]
            if not observations or all(item is None for item in value):
                return {
                    "status": "completed",
                    "section": section,
                    "coverage": "unavailable",
                    "reason": "Section was not captured in the current view",
                }
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
        offset = payload.get("offset", 0)
        return {
            "status": "completed",
            "section": section,
            "text": text[offset : offset + 6000],
            "next_offset": offset + 6000 if offset + 6000 < len(text) else None,
        }
