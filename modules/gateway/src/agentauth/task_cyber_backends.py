"""Bounded trusted cyber adapters. Samples are bytes to forward, never execute.

Task authorization and durable operation claims belong to task_cyber. Provider
identifiers live only under _job; the route removes that field from SDK output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config

MAX_SAMPLE = 64 * 1024 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_FINDINGS = 24000


class BackendUnavailableError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendUnavailableError("redirect_refused")


def _http(method, url, *, headers, data, timeout):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise BackendUnavailableError("response_too_large")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise BackendUnavailableError("invalid_response")
    return document


class CyberBackends:
    def __init__(self, env=None, clients=None, *, http=None, clock=time.time):
        self.env = os.environ if env is None else env
        self.clients = {} if clients is None else clients
        self.http = http or _http
        self.clock = clock
        self._secrets = set()

    def _client(self, name):
        if name not in self.clients:
            self.clients[name] = boto3.client(
                name,
                region_name=self.env.get("AWS_REGION", "us-east-1"),
                config=Config(
                    connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 1}, signature_version="s3v4" if name == "s3" else None
                ),
            )
        return self.clients[name]

    def _timeout(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise BackendUnavailableError("deadline_elapsed")
        return min(8.0, remaining)

    def _secret(self, name):
        reference = self.env.get(name)
        if not reference:
            raise BackendUnavailableError("credential_not_configured")
        value = self._client("secretsmanager").get_secret_value(SecretId=reference)["SecretString"]
        if not isinstance(value, str) or not value or len(value) > 8192:
            raise BackendUnavailableError("credential_unavailable")
        # Existing CAPE and VT Secrets Manager values are plain tokens.
        self._secrets.add(value)
        return value

    @staticmethod
    def _endpoint(value, path):
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BackendUnavailableError("endpoint_invalid")
        return value.rstrip("/") + path

    def _safe(self, value, depth=0):
        if depth > 6:
            return "[bounded]"
        if isinstance(value, dict):
            return {
                str(k)[:128]: self._safe(v, depth + 1)
                for k, v in list(value.items())[:80]
                if not re.search(r"secret|token|authorization|password|presign|download_url", str(k), re.I)
            }
        if isinstance(value, list):
            return [self._safe(v, depth + 1) for v in value[:40]]
        if isinstance(value, str):
            if re.search(r"X-Amz-(Signature|Credential|Security-Token)=", value, re.I):
                return "[signed URL omitted]"
            for secret in self._secrets:
                value = value.replace(secret, "[credential omitted]")
            return value[:2048]
        if value is None or type(value) in {bool, int, float}:
            return value
        return str(value)[:200]

    def _findings(self, value):
        safe = self._safe(value)
        if len(json.dumps(safe, ensure_ascii=False).encode()) > MAX_FINDINGS:
            return {"partial": True, "reason": "findings_exceed_output_bound"}
        return safe

    @staticmethod
    def _partial(kind, reason, **extra):
        return {"status": "partial", "kind": kind, "reason": reason, **extra}

    def submit(self, stage, job_id, sample, options, deadline_epoch):
        if stage not in {"triage", "static", "dynamic"}:
            return self._partial(stage, "unsupported_stage")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job_id):
            raise ValueError("invalid job identifier")
        if not sample.get("version") or sample["version"] == "null" or not re.fullmatch(r"[0-9a-f]{64}", sample.get("sha256", "")):
            raise ValueError("sample must be version and digest pinned")
        if type(sample.get("size")) is not int or not 0 < sample["size"] <= MAX_SAMPLE:
            raise ValueError("sample size invalid")
        if set(options) - {"focus", "yara_rules"} or any(
            not isinstance(v, list) or len(v) > 20 or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", x) for x in v)
            for v in options.values()
        ):
            raise ValueError("analysis options invalid")
        job = {"job_id": job_id, "kind": stage, "sample": sample, "deadline_epoch": deadline_epoch}
        if stage == "dynamic":
            return self._cape_submit(job)
        queue = self.env.get("CYBER_" + stage.upper() + "_QUEUE")
        if not queue:
            return self._partial(stage, "queue_not_configured", job_id=job_id)
        sent = False
        try:
            self._timeout(deadline_epoch)
            ttl = min(900, int(deadline_epoch - self.clock()))
            if ttl < 1:
                raise BackendUnavailableError("deadline_elapsed")
            url = self._client("s3").generate_presigned_url(
                "get_object", Params={"Bucket": sample["bucket"], "Key": sample["key"], "VersionId": sample["version"]}, ExpiresIn=ttl
            )
            manifest = {
                "artifact_id": job_id,
                "source_artifact_id": job_id,
                "stage": stage,
                "org_id": sample["org_id"],
                "team_id": sample["team_id"],
                "user_id": sample["user_id"],
                "sample_s3_uri": sample["sample_s3_uri"],
                "sample_download": {"url": url, "sha256": sample["sha256"], "size": sample["size"], "version": sample["version"]},
                "issued_at": int(self.clock()),
                "expires_at": int(self.clock()) + ttl,
                "focus": options.get("focus", []),
                "yara_rules": options.get("yara_rules", []),
                "registration_version": 1,
            }
            sent = True
            self._client("sqs").send_message(QueueUrl=queue, MessageBody=json.dumps(manifest), MessageGroupId=job_id, MessageDeduplicationId=job_id)
            return {"status": "pending", "kind": stage, "job_id": job_id, "_job": job}
        except Exception:
            # Sending may have succeeded. Parent keeps its durable operation claim.
            return {
                "status": "unknown" if sent else "partial",
                "kind": stage,
                "job_id": job_id,
                "reason": "submission_outcome_unavailable",
                "_job": job,
            }

    def _cape_submit(self, job):
        endpoint = self.env.get("CYBER_CAPE_ALB")
        if not endpoint or not self.env.get("CYBER_CAPE_TOKEN_SECRET"):
            return self._partial("dynamic", "cape_not_configured", job_id=job["job_id"])
        sent = False
        try:
            deadline, sample = job["deadline_epoch"], job["sample"]
            self._timeout(deadline)
            token = self._secret("CYBER_CAPE_TOKEN_SECRET")
            obj = self._client("s3").get_object(Bucket=sample["bucket"], Key=sample["key"], VersionId=sample["version"])
            # Bound and hash before upload; no sample parser, disk write or execution.
            chunks, digest, size = [], hashlib.sha256(), 0
            with obj["Body"] as stream:
                while chunk := stream.read(65536):
                    self._timeout(deadline)
                    size += len(chunk)
                    if size > sample["size"]:
                        raise BackendUnavailableError("sample_size_changed")
                    chunks.append(chunk)
                    digest.update(chunk)
            if size != sample["size"] or digest.hexdigest() != sample["sha256"]:
                raise BackendUnavailableError("sample_digest_changed")
            boundary = "adp" + uuid.uuid4().hex
            timeout = max(1, min(300, int(deadline - self.clock())))
            prefix = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="timeout"\r\n\r\n{timeout}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.bin"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            data = prefix + b"".join(chunks) + f"\r\n--{boundary}--\r\n".encode()
            url = self._endpoint(endpoint, "/apiv2/tasks/create/file/")
            request_timeout = self._timeout(deadline)
            sent = True
            result = self.http(
                "POST",
                url,
                headers={"Authorization": "Bearer " + token, "Content-Type": "multipart/form-data; boundary=" + boundary},
                data=data,
                timeout=request_timeout,
            )
            provider_id = result.get("data", {}).get("task_id", result.get("task_id"))
            if isinstance(provider_id, bool) or not re.fullmatch(r"[0-9]{1,12}", str(provider_id)):
                raise BackendUnavailableError("cape_submission_receipt_unavailable")
            job = {**job, "provider_job_id": str(provider_id), "backend_endpoint": endpoint}
            return {"status": "pending", "kind": "dynamic", "job_id": job["job_id"], "_job": job}
        except Exception:
            return {
                "status": "unknown" if sent else "partial",
                "kind": "dynamic",
                "job_id": job["job_id"],
                "reason": "cape_submission_outcome_unavailable" if sent else "cape_prerequisite_unavailable",
                "_job": job,
            }

    def result(self, job):
        kind = job["kind"]
        if kind == "dynamic":
            return self._cape_result(job)
        table = self.env.get("CYBER_RESULTS_TABLE")
        if not table:
            return self._partial(kind, "results_not_configured")
        try:
            # Observing completion after expiry is necessary to settle safely; this
            # grants no additional execution time and never submits new work.
            observation_deadline = self.clock() + 8
            self._timeout(observation_deadline)
            rows = (
                self._client("dynamodb")
                .query(
                    TableName=table,
                    KeyConditionExpression="artifact_id = :id",
                    ExpressionAttributeValues={":id": {"S": job["job_id"]}},
                    ConsistentRead=True,
                    ScanIndexForward=False,
                    Limit=1,
                )
                .get("Items", [])
            )
            if not rows:
                return {"status": "pending", "kind": kind, "job_id": job["job_id"]}
            row = {k: TypeDeserializer().deserialize(v) for k, v in rows[0].items()}
            if any(row.get(k) != job["sample"][k] for k in ("org_id", "team_id", "user_id")) or row.get("stage") != kind:
                raise BackendUnavailableError("result_scope_mismatch")
            findings = json.loads(row["findings"])
            status = row.get("status")
            if status not in {"completed", "complete", "success", "failed", "error", "rejected", "ok"}:
                raise BackendUnavailableError("result_status_unknown")
            return {
                "status": "completed" if status in {"completed", "complete", "success", "ok"} else "failed",
                "kind": kind,
                "job_id": job["job_id"],
                "findings": self._findings(findings),
            }
        except Exception:
            return self._partial(kind, "result_unavailable", job_id=job["job_id"])

    def _cape_result(self, job):
        provider_id = str(job.get("provider_job_id", ""))
        if not re.fullmatch(r"[0-9]{1,12}", provider_id):
            return self._partial("dynamic", "provider_job_identity_unavailable")
        execution_status = None
        try:
            # Observing completion after expiry is necessary to settle safely; this
            # grants no additional execution time and never submits new work.
            observation_deadline = self.clock() + 8
            self._timeout(observation_deadline)
            headers = {"Authorization": "Bearer " + self._secret("CYBER_CAPE_TOKEN_SECRET")}
            base = self.env.get("CYBER_CAPE_ALB", "")
            if job.get("backend_endpoint") != base:
                raise BackendUnavailableError("cape_endpoint_changed")
            status = self.http(
                "GET",
                self._endpoint(base, f"/apiv2/tasks/status/{provider_id}/"),
                headers=headers,
                data=None,
                timeout=self._timeout(observation_deadline),
            )
            state = status.get("data", {}).get("status")
            if state in {"failed", "error"}:
                return {"status": "failed", "execution_status": "failed", "kind": "dynamic", "job_id": job["job_id"]}
            if state not in {"reported", "completed", "pending", "running", "distributed", "processing"}:
                raise BackendUnavailableError("cape_status_unavailable")
            if state not in {"reported", "completed"}:
                return {"status": "pending", "kind": "dynamic", "job_id": job["job_id"]}
            execution_status = "completed"
            report = self.http(
                "GET",
                self._endpoint(base, f"/apiv2/tasks/report/{provider_id}/"),
                headers=headers,
                data=None,
                timeout=self._timeout(observation_deadline),
            )
            report = report.get("data", report)
            if not isinstance(report, dict) or not any(
                key in report for key in ("info", "signatures", "behavior", "network", "malscore", "detections")
            ):
                raise BackendUnavailableError("cape_report_unavailable")
            findings = {key: report[key] for key in ("info", "signatures", "behavior", "network", "malscore", "detections") if key in report}
            return {
                "status": "completed",
                "execution_status": execution_status,
                "kind": "dynamic",
                "job_id": job["job_id"],
                "findings": self._findings(findings),
            }
        except Exception:
            return self._partial(
                "dynamic", "cape_result_unavailable", job_id=job["job_id"], **({"execution_status": execution_status} if execution_status else {})
            )

    def cancel(self, job):
        # SQS visibility/deletion and CAPE report deletion do not prove execution stopped.
        return {"status": "unknown", "kind": job["kind"], "job_id": job["job_id"], "reason": "backend_cancellation_not_supported"}

    def url_analysis(self, url, deadline_epoch):
        endpoint = self.env.get("TASK_CYBER_BROWSER_ENDPOINT")
        if not endpoint:
            return self._partial("url_analysis", "browser_not_configured")
        sent = False
        try:
            timeout = self._timeout(deadline_epoch)
            broker_url = self._endpoint(endpoint, "/v1/analyze")
            sent = True
            document = self.http(
                "POST",
                broker_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(
                    {"url": url, "wait_until": "domcontentloaded", "timeout_ms": max(1, int(timeout * 1000) - 500), "ignore_https_errors": False}
                ).encode(),
                timeout=timeout,
            )
            if "error" in document:
                return self._partial("url_analysis", "browser_analysis_refused")
            return {"status": "completed", "kind": "url_analysis", "findings": self._findings(document)}
        except Exception:
            return {"status": "unknown" if sent else "partial", "kind": "url_analysis", "reason": "browser_analysis_outcome_unavailable"}

    def enrich(self, sha256, deadline_epoch):
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("invalid SHA256")
        if not self.env.get("CYBER_VT_TOKEN_SECRET"):
            return self._partial("enrichment", "virustotal_not_configured")
        try:
            self._timeout(deadline_epoch)
            token = self._secret("CYBER_VT_TOKEN_SECRET")
            result = self.http(
                "GET",
                "https://www.virustotal.com/api/v3/files/" + sha256,
                headers={"x-apikey": token},
                data=None,
                timeout=self._timeout(deadline_epoch),
            )
            attributes = result.get("data", {}).get("attributes", {})
            if not isinstance(attributes, dict) or not attributes:
                raise BackendUnavailableError("virustotal_result_unavailable")
            findings = {
                key: attributes[key]
                for key in ("last_analysis_stats", "last_analysis_date", "popular_threat_classification", "type_description", "reputation")
                if key in attributes
            }
            return {"status": "completed", "kind": "enrichment", "sha256": sha256, "findings": self._findings(findings)}
        except urllib.error.HTTPError as error:
            return self._partial("enrichment", "hash_not_found" if error.code == 404 else "virustotal_unavailable")
        except Exception:
            return self._partial("enrichment", "virustotal_unavailable")
