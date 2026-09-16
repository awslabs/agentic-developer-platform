#!/usr/bin/env python3
"""E08 on the instance: real Claude and Codex inference through ADP.

Step 4 of the executable path, and the one that cannot be faked. Everything
before it proves configuration; this proves the configuration carries a real
model request to the destination account that E06 provisioned.

The assertion is a three-way correlation, because any single leg can be green
while the routing is wrong:

1. The model returns a marker unique to this run. A cached or canned response
   cannot contain it, so this proves a real completion happened.
2. ADP's own usage log records that request with `bedrock_account_id` equal to
   the destination account. This proves ADP believes it routed cross-account.
3. CloudTrail in the destination account records a Bedrock invocation by the
   destination role. This proves the request actually landed there, rather than
   being served by the platform account while ADP merely labelled it otherwise.

Claude and Codex reach the gateway differently and both paths are exercised:
Claude Code calls `adp token` per request through `apiKeyHelper`, while Codex
talks to the local proxy `adp serve` starts. A run that only tested one would
miss the setup command for the other entirely.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

import common
from common import require

# The marker the model is asked to echo. Distinctive enough that it cannot occur
# by chance and cannot be produced without a completion actually running.
MARKER_PROMPT = (
    "Reply with exactly this token and nothing else, no punctuation, no "
    "explanation: {marker}"
)


def _session(config, home):
    """Materialize the session install_auth established, in an isolated HOME."""
    directory = home / ".bedrock-gateway"
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "gateway_url": config["gateway_url"],
                "refresh_via": config.get("refresh_via", "gateway"),
            }
        )
    )
    tokens = directory / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                "access_token": config["access_token"],
                "id_token": config.get("id_token", ""),
                "refresh_token": config.get("refresh_token", ""),
                "expires_at": config["session_expires_at"],
            }
        )
    )
    tokens.chmod(0o600)
    (directory / "config.json").chmod(0o600)


def _usage_record(config, marker, *, after):
    """Find this run's request in ADP's own usage log.

    Polled because usage is written asynchronously after the response returns; a
    single immediate read would flake on a system that is behaving correctly. The
    window is bounded by `usage_wait_seconds` and a miss is a failure, never a
    pass with a note.
    """

    def look():
        _status, payload = common.api(
            config,
            "/usage/logs?org_id="
            + config["org_id"]
            + "&user_id="
            + config["test_user_id"]
            + "&limit=100",
            config["access_token"],
        )
        for row in (payload or {}).get("items") or []:
            timestamp = str(row.get("timestamp") or "")
            if row.get("status_code") == 200 and timestamp >= after:
                return row
        return None

    return common.wait_for(
        look, timeout=int(config.get("usage_wait_seconds", 180)), interval=10
    )


def _cloudtrail_invocation(config, env, *, since):
    """A Bedrock invocation recorded in the DESTINATION account.

    Read with the destination profile, so a platform-account event cannot satisfy
    it. This is the leg that distinguishes "ADP says it routed cross-account"
    from "the request reached the other account".
    """

    def look():
        events = common.aws_cli(
            config,
            env,
            [
                "--profile",
                "destination",
                "cloudtrail",
                "lookup-events",
                "--lookup-attributes",
                "AttributeKey=EventSource,AttributeValue=bedrock.amazonaws.com",
                "--start-time",
                since,
                "--max-results",
                "50",
            ],
            missing_ok=True,
        )
        for event in (events or {}).get("Events") or []:
            name = event.get("EventName") or ""
            if name.startswith("Invoke") or name.startswith("Converse"):
                return {
                    "event_name": name,
                    "recorded_account": event.get("EventSource")
                    and config["destination_account"],
                    "username": event.get("Username"),
                }
        return None

    # CloudTrail delivery is minutes, not seconds; this bound reflects the
    # service's own SLA rather than optimism.
    return common.wait_for(
        look, timeout=int(config.get("cloudtrail_wait_seconds", 900)), interval=30
    )


def _claude(config, evidence, cli, env, home, marker):
    """Claude Code through `adp claude`, which refreshes via `adp token`."""
    evidence["stage"] = "claude_setup"
    code, _payload = cli.run(["claude", "setup"], expected=0, json_output=False)
    require(code == 0, "adp claude setup failed")
    settings = home / ".claude" / "settings.json"
    require(settings.is_file(), "adp claude setup did not write Claude settings")
    document = json.loads(settings.read_text())
    require(
        "apiKeyHelper" in document,
        "Claude settings have no apiKeyHelper; per-request refresh would not work",
    )
    evidence["claude_setup"] = {"api_key_helper_configured": True}

    # `adp token` is the helper Claude calls. Proving it returns a usable token
    # here separates a broken session from a broken model call.
    token_code, _ = cli.run(["token"], expected=0, json_output=False)
    require(
        token_code == 0, "adp token failed; Claude Code could not have authenticated"
    )

    evidence["stage"] = "claude_inference"
    argv = [
        str(config["cli_path"]),
        "claude",
        "--print",
        "--model",
        config["claude_model"],
        MARKER_PROMPT.format(marker=marker),
    ]
    evidence["transcript"].append(common.sanitize(argv))
    code, out, _err = common.bounded(
        argv, env=env, timeout=int(config.get("inference_timeout_seconds", 300))
    )
    require(code == 0, f"adp claude exited {code}; no completion was produced")
    require(
        marker in (out or ""),
        "The Claude completion did not contain this run's marker; the response was not a real completion of our prompt",
    )
    return {"returned_marker": True, "model": config["claude_model"]}


def _codex(config, evidence, cli, env, home, marker):
    """Codex through `adp codex`, which routes via the local `adp serve` proxy."""
    evidence["stage"] = "codex_setup"
    code, _payload = cli.run(["codex", "setup"], expected=0, json_output=False)
    require(code == 0, "adp codex setup failed")
    toml = home / ".codex" / "config.toml"
    require(toml.is_file(), "adp codex setup did not write a Codex config")
    text = toml.read_text()
    require(
        "adp-gateway" in text,
        "The Codex config does not name the ADP model provider; requests would bypass ADP",
    )
    port = re.search(r"base_url\s*=\s*\"http://127\.0\.0\.1:(\d+)", text)
    evidence["codex_setup"] = {
        "provider_configured": True,
        "proxy_port": int(port.group(1)) if port else None,
    }

    evidence["stage"] = "codex_inference"
    argv = [
        str(config["cli_path"]),
        "codex",
        "exec",
        "--skip-git-repo-check",
        MARKER_PROMPT.format(marker=marker),
    ]
    evidence["transcript"].append(common.sanitize(argv))
    code, out, _err = common.bounded(
        argv, env=env, timeout=int(config.get("inference_timeout_seconds", 300))
    )
    require(code == 0, f"adp codex exited {code}; no completion was produced")
    require(
        marker in (out or ""),
        "The Codex completion did not contain this run's marker",
    )
    # The proxy `adp codex` started is a child of this journey; leaving it running
    # would outlive the journey and hold the port against a resumed attempt on the
    # same instance. There is no `adp serve --stop` to ask for that — the core's
    # `serve` takes `--port` and `--foreground` only — so the pidfile it writes is
    # the handle, which is what `common.stop_proxy` uses.
    stopped = common.stop_proxy(home)
    require(
        stopped,
        "The auth proxy adp codex started could not be stopped; it would outlive "
        "this journey and hold its port against a resumed attempt",
    )
    return {"returned_marker": True, "proxy_stopped": True}


def execute(config, evidence):
    os.umask(0o077)
    require(
        config.get("access_token"),
        "personal_inference needs the session established by install_auth",
    )
    require(
        config.get("effective_destination_account")
        == str(config["destination_account"]),
        "The effective routing rule does not point at the destination account; "
        "E08 would not prove cross-account inference",
    )

    with tempfile.TemporaryDirectory(prefix="adp-inference-") as temporary:
        home = Path(temporary)
        env = common.clean_env(
            config,
            HOME=str(home),
            AWS_CONFIG_FILE=str(home / "aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
            # Keep the proxy off the default port so a resumed attempt on the
            # same instance cannot collide with a lingering listener.
            ADP_PROXY_PORT=str(config.get("proxy_port", 9191)),
        )
        (home / "aws-config").write_text(
            "[profile destination]\n"
            f"role_arn = {config['provisioner_arn']}\n"
            "credential_source = Ec2InstanceMetadata\n"
            f"region = {config['region']}\n"
        )
        _session(config, home)
        cli = common.Cli(Path(config["cli_path"]), env, evidence["transcript"])

        # Fail fast on the session before attributing a model failure to routing.
        status_code, _ = cli.run(["status"], expected=0, json_output=False)
        require(status_code == 0, "adp status reports no live session on this instance")

        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        claude_marker = config["evaluation_id"] + "-claude"
        codex_marker = config["evaluation_id"] + "-codex"

        evidence["claude"] = _claude(config, evidence, cli, env, home, claude_marker)
        evidence["codex"] = _codex(config, evidence, cli, env, home, codex_marker)
        evidence["checks"].extend(["claude_returned_marker", "codex_returned_marker"])

        # Leg 2: ADP's own record of the request, and the account it routed to.
        evidence["stage"] = "adp_usage"
        record = _usage_record(config, claude_marker, after=started)
        require(
            record is not None,
            "ADP recorded no successful usage for this run; inference cannot be attributed",
        )
        evidence["usage"] = {
            "model": record.get("model"),
            "bedrock_account_id": record.get("bedrock_account_id"),
            "status_code": record.get("status_code"),
            "input_tokens": record.get("input_tokens"),
            "output_tokens": record.get("output_tokens"),
            "request_id": record.get("request_id"),
        }
        require(
            str(record.get("bedrock_account_id")) == str(config["destination_account"]),
            "ADP recorded the usage against "
            f"{record.get('bedrock_account_id')!r}, not the destination account",
        )
        require(
            int(record.get("output_tokens") or 0) > 0,
            "The usage record has no output tokens; no completion was billed",
        )
        evidence["checks"].append("adp_usage_names_destination_account")

        # Leg 3: the destination account's own audit trail.
        evidence["stage"] = "cloudtrail"
        trail = _cloudtrail_invocation(config, env, since=started)
        if trail is None:
            # A miss here is a real failure of the correlation, not a soft skip:
            # without it we cannot distinguish cross-account routing from a
            # platform-account call that ADP labelled as cross-account.
            raise common.RemoteError(
                "No Bedrock invocation appeared in the destination account's CloudTrail; "
                "cross-account routing is unproven"
            )
        evidence["cloudtrail"] = trail
        evidence["checks"].append("destination_account_cloudtrail_records_invocation")

        evidence["correlation"] = {
            "inference_request_id": record.get("request_id"),
            "inference_bedrock_account": record.get("bedrock_account_id"),
            "cloudtrail_event": trail.get("event_name"),
        }
        evidence["detail"] = {
            "claude": evidence["claude"],
            "codex": evidence["codex"],
            "usage": evidence["usage"],
            "cloudtrail": trail,
            "checks": evidence["checks"],
        }
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
