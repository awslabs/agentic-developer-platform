"""The seams between the orchestrator and everything outside the process.

Every AWS call, gateway request and on-instance command goes through one of these
three ports. That exists for two reasons:

1. The offline guards drive the SAME entry point Actions drives, with doubles
   substituted here, so the wiring itself is covered rather than only the pure
   grading logic. A stage mapping assembled by hand in a test proves nothing
   about the mapping the workflow gets.
2. Production defaults are live. `default_ports()` returns boto3 and urllib
   implementations; a test must pass doubles explicitly. The dangerous default is
   the other way round — a harness that silently no-ops when a dependency is
   missing reports success for work it never did.

Nothing here interprets a result. These are transports; the assertions live in
`stages.py` so that what counts as a pass is never a property of the transport.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class PortError(RuntimeError):
    """A transport failed. Carries no provider text, which can quote a token."""


class AwsPort:
    """Lazily-created boto3 clients, per (service, account) role session.

    Lazy because importing/creating clients at module import time would make the
    offline suite depend on boto3 being installed and on a metadata endpoint
    answering.
    """

    def __init__(self, region, *, session=None, endpoints=None):
        self.region = region
        self.endpoints = endpoints or {}
        self._session = session
        self._clients = {}

    def session(self):
        if self._session is None:
            import boto3

            self._session = boto3.session.Session(region_name=self.region)
        return self._session

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = self.session().client(
                service,
                region_name=self.region,
                endpoint_url=self.endpoints.get(service),
            )
        return self._clients[service]

    def assume(self, role_arn, session_name, *, duration_seconds=3600):
        """A second port whose calls use a short-lived assumed-role session.

        Cross-account evidence has to come from the other account's own session:
        calling STS twice on one session proves nothing about access to the
        destination account, it just repeats what the runner already knew.
        """
        credentials = self.call(
            "sts",
            "assume_role",
            RoleArn=role_arn,
            RoleSessionName=session_name[:64],
            DurationSeconds=duration_seconds,
        )["Credentials"]
        import boto3

        session = boto3.session.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self.region,
        )
        return AwsPort(self.region, session=session, endpoints=self.endpoints)

    def call(self, service, operation, **kwargs):
        """Invoke one API operation. Raises PortError with the AWS error CODE only."""
        client = self.client(service)
        try:
            return getattr(client, operation)(**kwargs)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            raise PortError(
                f"{service}.{operation} failed: {code or type(exc).__name__}"
            ) from None


class HttpPort:
    """Gateway HTTP, refusing redirects before a credential is sent."""

    def __init__(self, *, timeout=60):
        self.timeout = timeout

    def request(self, url, *, method="GET", token=None, body=None, expect=200):
        """One gateway request. Returns (status, parsed-json-or-None).

        `expect=None` returns the status instead of raising, which is what a
        cleanup path needs: a 404 on a resource we are deleting is the desired end
        state, not an error.
        """

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                raise PortError(f"{url} redirected before credentials were sent")

        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if token:
            request.add_header("Authorization", "Bearer " + token)
        if data:
            request.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise PortError(f"{url} unreachable: {type(exc).__name__}") from None
        if expect is not None and status != expect:
            raise PortError(f"{url} returned HTTP {status}, expected {expect}")
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, None

    def get(self, url, *, token=None, expect=200):
        return self.request(url, method="GET", token=token, expect=expect)

    def get_bytes(self, url, *, expect=200):
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                if response.status != expect:
                    raise PortError(f"{url} returned HTTP {response.status}")
                return response.read()
        except urllib.error.HTTPError as exc:
            raise PortError(f"{url} returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise PortError(f"{url} unreachable: {type(exc).__name__}") from None


class SsmPort:
    """Runs shell on the disposable instance and waits, bounded.

    This is the ONLY way a product CLI command is executed. Actions never runs
    `adp` itself: the runner has provider environment baked in, which would
    silently substitute the platform's own auth for the auth path under test.
    """

    def __init__(self, aws, *, clock=None, sleep=None):
        self.aws = aws
        import time as _time

        self.clock = clock or _time.monotonic
        self.sleep = sleep or _time.sleep

    def run(self, instance_id, commands, *, purpose, timeout=600):
        """Send, poll to a terminal state, return the invocation.

        Cancels the command if we give up, so an abandoned command cannot keep
        mutating the account after the orchestrator has stopped watching.
        """
        command_id = self.aws.call(
            "ssm",
            "send_command",
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": list(commands),
                "executionTimeout": [str(timeout)],
            },
            TimeoutSeconds=timeout,
            Comment=f"cli-uplift-eval {purpose}"[:100],
        )["Command"]["CommandId"]
        deadline = self.clock() + timeout + 60
        try:
            while self.clock() < deadline:
                try:
                    result = self.aws.call(
                        "ssm",
                        "get_command_invocation",
                        CommandId=command_id,
                        InstanceId=instance_id,
                    )
                except PortError:
                    self.sleep(2)
                    continue
                if result.get("Status") not in ("Pending", "InProgress", "Delayed"):
                    return result
                self.sleep(3)
            raise PortError(f"SSM command {purpose} exceeded its deadline")
        except BaseException:
            try:
                self.aws.call(
                    "ssm",
                    "cancel_command",
                    CommandId=command_id,
                    InstanceIds=[instance_id],
                )
            except PortError:
                pass
            raise

    def json_result(self, instance_id, commands, *, purpose, timeout=600):
        """Run commands whose last line of stdout is a JSON document.

        The worker prints exactly one JSON object, so a parse failure means the
        worker never got far enough to report — which is a failure, never a pass.
        """
        result = self.run(instance_id, commands, purpose=purpose, timeout=timeout)
        for line in reversed((result.get("StandardOutputContent") or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return result, json.loads(line)
                except ValueError:
                    continue
        return result, None


def default_ports(cfg):
    """Live transports. Tests override; production must not have to opt in."""
    aws = AwsPort(
        cfg["region"],
        endpoints={
            "sts": cfg.get("sts_endpoint"),
            "secretsmanager": cfg.get("secrets_endpoint"),
        },
    )
    return {
        "aws": aws,
        "http": HttpPort(timeout=min(cfg.get("timeout_seconds", 240), 900)),
        "ssm": SsmPort(aws),
    }
