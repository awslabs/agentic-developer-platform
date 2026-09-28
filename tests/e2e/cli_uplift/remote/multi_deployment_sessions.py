#!/usr/bin/env python3
"""EC2 session checkpoint for #5413; does not satisfy E16/E17 model acceptance.

Run through dispatcher.py multi_deployment_sessions <payload.json>. Uses the same
three gateway/credential bindings as E16/E17, but invokes the real tools only with
--version. Useful for checking live authentication before model routing is ready.
"""

from __future__ import annotations

import concurrent.futures
import tempfile
from pathlib import Path

import common
import multi_deployment as multi


def execute(config, evidence):
    multi._validate_bindings(config)
    evidence.update(checkpoint_only=True, model_requests=0)
    with tempfile.TemporaryDirectory(prefix="adp-session-checkpoint-") as temporary:
        home, env = multi._home(config, temporary)
        cli = common.Cli(Path(config["cli_path"]), env, evidence["transcript"])
        identifiers = {}
        primary_failure = None
        try:
            multi._register(cli, config["deployments"], evidence, identifiers)
            evidence["stage"] = "login"
            sessions = {
                entry["name"]: multi._login(config, cli, env, entry, evidence)
                for entry in config["deployments"]
            }
            evidence["checks"].append("three_real_cognito_logins_one_home")
            for name in sessions:
                multi._setup_tools(cli, name, home)
            settings = [home / ".claude/settings.json", home / ".codex/config.toml"]
            before_settings = [path.read_bytes() for path in settings]
            evidence["stage"] = "launchers"
            evidence["launchers"] = []

            def launch(pair):
                name, tool = pair
                code, _out, _err = common.bounded(
                    [str(cli.binary), "--deployment", name, tool, "--version"],
                    env=env,
                    timeout=60,
                )
                common.require(code == 0, f"{name}/{tool} --version exited {code}")
                return {"deployment": name, "tool": tool, "exit_code": code}

            for arrangement in multi.ARRANGEMENTS.values():
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    evidence["launchers"].extend(
                        pool.map(launch, zip(sessions, arrangement))
                    )
            common.require(
                before_settings == [path.read_bytes() for path in settings],
                "Launchers changed shared tool settings",
            )
            proxies = multi._proxy_identities(home, identifiers)
            common.require(
                set(proxies) == set(sessions)
                and all(item["attributed_correctly"] for item in proxies.values())
                and len({item["port"] for item in proxies.values()}) == 3,
                "The three proxies do not have distinct, correctly attributed ports",
            )
            evidence["proxy_identities"] = proxies
            evidence["checks"].append(
                "real_tool_launchers_and_three_independent_proxies"
            )

            def query(name):
                token = multi._access_token(cli, name)
                status, actor = common.api(
                    {**config, "gateway_url": sessions[name]["gateway_url"]},
                    "/auth/cli/admin-session",
                    token,
                )
                common.require(
                    actor.get("verified")
                    and actor.get("user_id") == sessions[name]["user_id"],
                    f"{name} did not confirm the expected live identity",
                )
                return {
                    "deployment": name,
                    "user_id": actor["user_id"],
                    "status": status,
                }

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                evidence["concurrent_live_identities"] = list(pool.map(query, sessions))
            multi._lifecycle_changes(config, cli, sessions, evidence)
            evidence["surviving_live_identities"] = [
                query(name) for name in list(sessions)[1:]
            ]
            evidence["checks"].append(
                "surviving_tokens_accepted_by_real_gateways_after_logout"
            )
        except Exception as exc:
            primary_failure = exc
            evidence["failure_stage"] = evidence.get("stage")
            raise
        finally:
            if identifiers:
                try:
                    multi._teardown(cli, home, identifiers, evidence)
                except Exception as exc:
                    evidence["teardown_error"] = str(exc)
                    if primary_failure is None:
                        raise
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
