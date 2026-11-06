"""Dedicated fixture login and one-shot all-in admission for tools release smoke.

This client cannot approve a run, designate a fixture, create consent or mint a
human login. The gateway verifies the existing Cognito access JWT and immutable
owner approval. No refresh-token or model-only fallback is supported.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import hmac
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

OIDC_AUDIENCE = "adp-native-test-enrollment-v1"
CAPABILITY_HEADER = "x-adp-native-test-capability"
OPERATION_HEADER = "x-adp-native-test-operation"
MAX_RESPONSE = 262144
MAX_STATUS_READS = 3
JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class AdmissionUnavailable(ValueError):
    """Safe error only: never propagate credentials or a provider response body."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise AdmissionUnavailable(
            "Native test endpoint redirected; original run requires inspection"
        )


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdmissionUnavailable("Native test response is invalid")
        result[key] = value
    return result


def identifier(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise AdmissionUnavailable("Native test reference is invalid")
    return value


def origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in (None, 443)
        or parsed.path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AdmissionUnavailable(
            "Native smoke requires one HTTPS public gateway origin"
        )
    return value


def private_file(path, maximum):
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise AdmissionUnavailable(
                "Native test input must be one private regular file"
            )
        value = os.read(descriptor, maximum + 1)
        if not value or len(value) > maximum:
            raise AdmissionUnavailable("Native test input is missing or oversized")
        return value
    finally:
        os.close(descriptor)


def request_json(opener, url, headers, *, method="GET", payload=None):
    data = canonical(payload) if payload is not None else None
    if data is not None and len(data) > 65536:
        raise AdmissionUnavailable("Native test request is oversized")
    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            **headers,
        },
        method=method,
    )
    try:
        with opener.open(request, timeout=40) as response:
            if response.status != 200:
                raise AdmissionUnavailable(
                    "Native test operation was not confirmed; inspect the original run"
                )
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise AdmissionUnavailable("Native test response is oversized")
        result = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(result, dict):
            raise AdmissionUnavailable("Native test response is invalid")
        return result
    except HTTPError as error:
        error.close()
        raise AdmissionUnavailable(
            "Native test operation was refused; inspect the original run"
        ) from None
    except (OSError, URLError, ValueError, HTTPException):
        # POST may already have committed. Never retry, rotate an operation ID,
        # exchange again, parse an error body, or downgrade admission here.
        raise AdmissionUnavailable(
            "Native test response is unconfirmed; inspect the original run"
        ) from None


@dataclass(frozen=True)
class ApprovedReference:
    run_id: str
    tenant_id: str
    approval_revision: str
    deployment_id: str
    gateway_origin: str
    source_revision: str
    definition_revision: str
    github_run_id: str
    github_run_attempt: str

    @classmethod
    def read(cls, path, environ):
        value = json.loads(private_file(path, 16384), object_pairs_hook=unique_object)
        expected = {"version", *cls.__dataclass_fields__}
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or type(value.get("version")) is not int
            or value.pop("version") != 1
            or any(not isinstance(item, str) for item in value.values())
        ):
            raise AdmissionUnavailable(
                "Immutable native test approval reference is required"
            )
        result = cls(**value)
        identifier(result.run_id)
        identifier(result.approval_revision)
        origin(result.gateway_origin)
        if (
            any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value)
                for value in (result.tenant_id, result.deployment_id)
            )
            or not re.fullmatch(r"[0-9a-f]{40}", result.source_revision)
            or not re.fullmatch(r"[0-9a-f]{40}", result.definition_revision)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", result.github_run_id)
            or result.github_run_attempt != "1"
        ):
            raise AdmissionUnavailable("Native test approval binding is invalid")
        bindings = {
            "ADP_SOURCE_REVISION": result.source_revision,
            "ADP_DEFINITION_REVISION": result.definition_revision,
            "GITHUB_RUN_ID": result.github_run_id,
            "GITHUB_RUN_ATTEMPT": result.github_run_attempt,
            "ADP_TOOLS_SMOKE_GATEWAY_URL": result.gateway_origin,
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
        }
        if any(environ.get(key) != value for key, value in bindings.items()):
            raise AdmissionUnavailable(
                "Native smoke requires the original approved direct workflow job"
            )
        return result


def producer_proof(reference, oidc_token, environ, *, now=None):
    """Sign the existing fixed STS proof with the job's frozen role credentials.

    No SDK credential lookup, role assumption, credential persistence or STS
    request is performed here. The gateway verifies this exact signed envelope.
    """
    region = environ.get("ADP_TOOLS_AWS_REGION", "")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region):
        raise AdmissionUnavailable("Native test producer region is unavailable")
    access, secret, session = (
        environ.get(key, "")
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    )
    if any(
        not value or any(character.isspace() for character in value)
        for value in (access, secret, session)
    ):
        raise AdmissionUnavailable(
            "Native test requires the authenticated job role credentials"
        )
    envelope = {
        "tenant_id": reference.tenant_id,
        "run_id": reference.run_id,
        "approval_revision": reference.approval_revision,
        "github_oidc_token": oidc_token,
        "deployment_id": reference.deployment_id,
    }
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "host": f"sts.{region}.amazonaws.com",
        "x-amz-date": timestamp,
        "x-amz-security-token": session,
        "x-adp-work-invocation": hashlib.sha256(canonical(envelope)).hexdigest(),
    }
    names = ";".join(sorted(headers))
    body = b"Action=GetCallerIdentity&Version=2011-06-15"
    signed = (
        "POST\n/\n\n"
        + "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
        + "\n"
        + names
        + "\n"
        + hashlib.sha256(body).hexdigest()
    )
    scope = f"{timestamp[:8]}/{region}/sts/aws4_request"
    to_sign = (
        "AWS4-HMAC-SHA256\n"
        + timestamp
        + "\n"
        + scope
        + "\n"
        + hashlib.sha256(signed.encode()).hexdigest()
    )
    key = ("AWS4" + secret).encode()
    for part in (timestamp[:8], region, "sts", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={names}, Signature={signature}"
    )
    del headers["host"]  # Verifier fixes the STS endpoint and reconstructs Host.
    proof = base64.b64encode(canonical(headers)).decode()
    if len(proof) > 12000:
        raise AdmissionUnavailable("Native test producer proof is oversized")
    return proof


def github_oidc(opener, environ):
    parsed = urlsplit(environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""))
    token = environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".actions.githubusercontent.com")
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not token
        or any(character.isspace() for character in token)
    ):
        raise AdmissionUnavailable("Protected GitHub enrollment proof is unavailable")
    query = [
        (key, value) for key, value in parse_qsl(parsed.query) if key != "audience"
    ]
    url = urlunsplit(
        parsed._replace(query=urlencode([*query, ("audience", OIDC_AUDIENCE)]))
    )
    value = request_json(opener, url, {"Authorization": "Bearer " + token}).get("value")
    if not isinstance(value, str) or len(value) > 16384 or not JWT.fullmatch(value):
        raise AdmissionUnavailable("Protected GitHub enrollment proof is invalid")
    return value


@dataclass
class NativeSession:
    reference: ApprovedReference
    token: str = field(repr=False)
    capability: str = field(repr=False)
    opener: object = field(repr=False)
    operations: set[str] = field(default_factory=set, repr=False)
    status_reads: int = field(default=0, repr=False)

    @classmethod
    def exchange(cls, environ=None, *, opener=None):
        environ = dict(os.environ if environ is None else environ)
        reference = ApprovedReference.read(
            environ["ADP_TOOLS_SMOKE_APPROVAL_FILE"], environ
        )
        token = (
            private_file(environ["ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE"], 16384)
            .decode()
            .strip()
        )
        if not JWT.fullmatch(token):
            raise AdmissionUnavailable("Dedicated Cognito access login is required")
        opener = opener or build_opener(ProxyHandler({}), NoRedirects())
        oidc = github_oidc(opener, environ)
        proof = producer_proof(reference, oidc, environ)
        result = request_json(
            opener,
            reference.gateway_origin + "/api/v1/native-tests/capabilities/exchange",
            {"Authorization": "Bearer " + token},
            method="POST",
            payload={
                "run_id": reference.run_id,
                "github_oidc_token": oidc,
                "producer_proof": proof,
            },
        )
        capability = result.get("capability")
        if (
            result.get("scope") != "all_in"
            or result.get("all_in_admission") != "required"
            or not isinstance(capability, str)
            or not re.fullmatch(r"adpt1\.[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}", capability)
        ):
            raise AdmissionUnavailable(
                "Authoritative all-in native test admission is unavailable"
            )
        identifier(capability.split(".")[1])
        return cls(reference, token, capability, opener)

    def status(self):
        """Read only this admitted run within its protected setup allowance.

        Status reads do not create paid operation identities or exchange a new
        capability. An unconfirmed attempt still consumes the local read limit.
        """
        if self.status_reads >= MAX_STATUS_READS:
            raise AdmissionUnavailable(
                "Native status read limit reached; inspect the original run"
            )
        self.status_reads += 1
        return request_json(
            self.opener,
            self.reference.gateway_origin
            + "/api/v1/native-tests/runs/"
            + identifier(self.reference.run_id)
            + "/status",
            {
                "Authorization": "Bearer " + self.token,
                CAPABILITY_HEADER: self.capability,
            },
        )

    def export_client_transport(self, path, *, calls, models, max_discoveries, expected_principal=None):
        """Deliver this already exchanged original session to isolated clients.

        Client request limits are local transport guards. They neither replace
        protected server price contracts nor establish an aggregate reservation.
        The status read uses the original finite setup allowance.
        """
        if (not isinstance(calls, list) or not calls or not isinstance(models, list) or
                type(max_discoveries) is not int or not 1 <= max_discoveries <= 9):
            raise AdmissionUnavailable("Explicit finite native client transport plan is required")
        status = self.status()
        if expected_principal is not None and status.get("principal") != expected_principal:
            raise AdmissionUnavailable("Original native approval belongs to another owner")
        started = status.get("run_started_at")
        if (status.get("run_id") != self.reference.run_id or status.get("funding_status") != "held" or
                status.get("cap_usd") != "20" or type(started) is not int or
                not started <= datetime.now(UTC).timestamp() < started + 300):
            raise AdmissionUnavailable("Original native setup funding is unconfirmed")
        value = {
            "version": 1, "reference": asdict(self.reference), "access_token": self.token, "capability": self.capability,
            "started_at": started, "calls": calls, "models": models, "max_discoveries": max_discoveries,
        }
        encoded = canonical(value)
        if len(encoded) > 65536:
            raise AdmissionUnavailable("Native client transport plan is oversized")
        target = Path(path)
        try:
            metadata = target.parent.lstat()
            if (not target.is_absolute() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or
                    metadata.st_mode & 0o077):
                raise ValueError
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        except (OSError, ValueError):
            raise AdmissionUnavailable("Private original native client transport cannot be created") from None
        return target

    def request(
        self, method, path, *, payload=None, gateway_url=None, operation_id=None
    ):
        parsed = urlsplit(path)
        if (
            method not in {"GET", "POST"}
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not (
                parsed.path == "/api/agent/tools"
                or parsed.path.startswith("/api/agent/tools/")
            )
            or any(part in {".", ".."} for part in parsed.path.split("/"))
            or "%" in parsed.path
        ):
            raise AdmissionUnavailable("Native smoke route is unsupported")
        operation = identifier(operation_id) if operation_id else str(uuid4())
        if operation in self.operations:
            raise AdmissionUnavailable(
                "Native operation already attempted; inspect its original outcome"
            )
        self.operations.add(operation)
        headers = {
            "Authorization": "Bearer " + self.token,
            CAPABILITY_HEADER: self.capability,
            OPERATION_HEADER: operation,
        }
        if gateway_url is not None:
            headers["x-adp-expected-gateway-sha256"] = hashlib.sha256(
                gateway_url.encode()
            ).hexdigest()
        return request_json(
            self.opener,
            self.reference.gateway_origin + path,
            headers,
            method=method,
            payload=payload,
        )
