#!/usr/bin/env python3
"""E16/E17 on the instance: one installed CLI, three real ADP deployments.

This is the live half of #5413. The deterministic half already exists
(`modules/gateway/tests/cli/test_adp_three_gateways.py`) and proves the same
routing property against three recording gateways on loopback. What only an
instance can add is real Cognito logins, real Claude and Codex processes, and the
product's own usage log as the receipt.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER JOURNEY HERE

Every other journey materializes one `~/.bedrock-gateway` and one session. This
one registers three deployments in ONE home directory and signs in to each
separately, because the defect class under test only exists when the state is
shared. A journey that gave each deployment its own HOME would pass while the
product mixed all three up — HOME isolation is the very thing the CLI is supposed
to make unnecessary.

HOW A CROSSED REQUEST IS DETECTED

Not from what the CLI prints. A command that reports success while sending
integration's token to the development gateway looks identical to a correct one,
so the load-bearing assertions are made against the deployments themselves:

* each tool request ID must appear in the usage log of ITS OWN deployment, recorded
  against the identity that signed in there;
* the same request ID must be ABSENT from the other two deployments' usage logs, read
  with each of those deployments' OWN credential, so an absence is that
  deployment's own account of what it did not receive rather than an inference
  from the first one.

BOUNDS

The issue's live limits are hard: one instance, 48 requests, 256 output tokens per
request. Six tool sessions are launched for overlap (three in each arrangement). The
lifecycle case launches three sessions and continues them after state changes.
Claude receives the configured output cap; Codex is prompted for a short reply.
The existing run-level cost and time controls remain required.

WHAT IS DELIBERATELY NOT DONE

No deployment is created, deleted or reconfigured. The three gateways are supplied
fixtures; this journey only registers them locally, signs in, runs tools, and
removes its own local records at the end. `adp deployment remove` is local-only by
design, so teardown cannot affect a real environment.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import uuid
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import common
from common import require

# Keys with no sensible on-instance default. Named here so a payload gap fails
# saying which key is absent, rather than as a KeyError attributed to routing.
REQUIRED = (
    "mode",
    "instance_id",
    "platform_account",
    "region",
    "deployments",
    "evaluation_id",
    "secrets_endpoint",
    "sts_endpoint",
    "cli_path",
    "claude_model",
)

# Reply-with-exactly-this prompt. The marker carries the deployment name and the
# tool, so a response that surfaces in the wrong deployment's usage log is
# unambiguous about which pair crossed.
MARKER_PROMPT = (
    "Reply with exactly this token and nothing else, no punctuation, no "
    "explanation: {marker}"
)

# Which tool each deployment runs, by position. The issue asks for "two Codex +
# Claude and reverse mix", so the second pass swaps them. One arrangement alone
# would miss a per-tool pinning defect, and the two tools reach ADP by genuinely
# different mechanisms — Claude calls `adp token` per request through
# `apiKeyHelper`, Codex talks to a loopback proxy started at launch — so which
# deployment uses which is not cosmetic.
ARRANGEMENTS = {
    "overlap": ("codex", "codex", "claude"),
    "reverse": ("claude", "claude", "codex"),
}


def _home(config, temporary):
    """The ONE home directory all three deployments share.

    Shared on purpose, and it is the whole point of the case: three deployments in
    three homes would be isolated by the filesystem rather than by the product, so
    the journey would prove nothing about the CLI. `clean_env` strips every
    inherited AWS/ADP/ANTHROPIC/CODEX variable, so no ambient credential or
    endpoint can stand in for the selection under test.

    ADP_PROXY_PORT is deliberately NOT set. It pins the proxy to one fixed port,
    and three concurrent Codex sessions is exactly the case that cannot share a
    port — leaving it unset is what makes each named deployment ask the OS for its
    own, which is the AC-04 behaviour under test.
    """
    home = Path(temporary)
    env = common.clean_env(
        config,
        HOME=str(home),
        AWS_CONFIG_FILE=str(home / "aws-config"),
        AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
        # The issue's per-request output cap, applied to the tool rather than
        # trusted to the prompt: a model that ignores "reply with only this token"
        # must still not be able to spend more than the limit allows.
        CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(config.get("max_output_length", 256)),
    )
    return home, env


def _register(cli, deployments, evidence, identifiers=None):
    """`adp deployment add` for each, then prove the CLI agrees it has three.

    Asserted through `adp deployment list --json` rather than by reading the
    registry file: the file is an implementation detail, and what a user's next
    command resolves against is what the CLI reports.
    """
    evidence["stage"] = "register"
    if identifiers is None:
        identifiers = {}
    for entry in deployments:
        cli.json(["deployment", "add", entry["name"], "--url", entry["gateway_url"]])
        # Preserve partial progress for cleanup if a later registration fails.
        for row in cli.json(["deployment", "list"]).get("deployments") or []:
            if row.get("name") in {item["name"] for item in deployments}:
                identifiers[row["name"]] = row["deployment_id"]

    # `deployment list` prints the listing document directly, not the
    # status/detail envelope the mutating verbs use.
    listed = cli.json(["deployment", "list"])
    records = {str(row.get("name")): row for row in listed.get("deployments") or []}
    missing = [entry["name"] for entry in deployments if entry["name"] not in records]
    require(not missing, "adp deployment list does not report: " + ", ".join(missing))

    # Distinct stable ids are what every per-deployment store, lock and proxy
    # identity keys on. Three records sharing one id would be aliases of a single
    # deployment, and the isolation under test would not exist to be proven.
    identifiers = [
        str(records[entry["name"]].get("deployment_id") or "") for entry in deployments
    ]
    require(all(identifiers), "A registered deployment has no stable id")
    require(
        len(set(identifiers)) == len(identifiers),
        "Two registered deployments share a stable id, so they share a session "
        "store; they are aliases rather than independent deployments",
    )
    evidence["deployments"] = {
        "registered": [entry["name"] for entry in deployments],
        "distinct_ids": len(set(identifiers)),
        "default": listed.get("default"),
    }
    return dict(zip([entry["name"] for entry in deployments], identifiers, strict=True))


def _access_token(cli, name):
    """That deployment's own bearer token, via the product's own `adp token`.

    Asking for it with `--deployment` also checks that the token store is
    per-deployment: a single shared store would hand back the same token three
    times, and the cross-deployment absence assertions would then be silently
    comparing a deployment against itself.
    """
    code, out, _err = common.bounded(
        [str(cli.binary), "--deployment", name, "token"], env=cli.env, timeout=120
    )
    require(code == 0, f"`adp --deployment {name} token` failed; it has no session")
    token = (out or "").strip()
    require(token, f"adp token returned nothing for {name}")
    return token


def _login(config, cli, env, entry, evidence):
    """Sign in to ONE deployment, with that deployment's own fixture identity.

    The credential arrives on stdin, so it never reaches a process listing or the
    transcript, and it is read from that deployment's own Secrets Manager
    reference — a shared identity could not show that logging out of one
    deployment leaves the others signed in.
    """
    name = entry["name"]
    # `credential_secret` is the key `common.fixture_secret` reads, and its cache
    # is keyed on that name, so three deployments correctly read three secrets.
    scoped = {**config, "credential_secret": entry["credential_secret_name"]}
    credentials = {
        "username": common.fixture_secret(scoped, env, "admin_username"),
        "password": common.fixture_secret(scoped, env, "admin_password"),
    }
    payload = cli.json(
        ["--deployment", name, "admin", "login", "--credentials-stdin"],
        stdin_text=json.dumps(credentials),
    )
    require(
        payload.get("status") == "verified",
        f"adp admin login against {name} reported {payload.get('status')!r}",
    )

    # The identity the usage log keys on, read from the deployment itself rather
    # than assumed equal to the username: the receipt assertion must compare the
    # gateway's own notion of who signed in.
    _status, session = common.api(
        {**config, "gateway_url": entry["gateway_url"]},
        "/auth/cli/admin-session",
        _access_token(cli, name),
        expect=(200,),
    )
    require(
        (session or {}).get("verified"),
        f"{name} did not confirm an admin session for the identity that logged in",
    )
    require(
        (session or {}).get("org_id"),
        f"{name} attributed no organization to the signed-in identity; its usage "
        "log cannot be queried without one",
    )
    return {
        "name": name,
        "gateway_url": entry["gateway_url"],
        "user_id": str(session.get("user_id") or ""),
        "org_id": str(session.get("org_id") or ""),
    }


def _setup_tools(cli, name, home):
    """`codex setup` and `claude setup` for one deployment, asserted usable.

    Both tools are set up for every deployment because the reverse pass launches
    the other one, and a setup that only ran for the tool used first would make
    that pass test a stale configuration.

    Both tools keep ONE config file per HOME (`~/.claude/settings.json`,
    `~/.codex/config.toml`), which is why the LAUNCHER — not the setup file — is
    what pins a deployment at run time. So what is asserted here is that the setup
    wrote a usable ADP configuration at all; the per-request pin is proven later by
    the receipts and by each proxy's published identity, which is where it actually
    lives.
    """
    for verb in ("codex", "claude"):
        code, _payload = cli.run(
            ["--deployment", name, verb, "setup"], expected=0, json_output=False
        )
        require(code == 0, f"`adp --deployment {name} {verb} setup` failed")

    settings = home / ".claude" / "settings.json"
    require(settings.is_file(), "claude setup wrote no settings.json")
    require(
        "apiKeyHelper" in json.loads(settings.read_text()),
        "Claude settings carry no apiKeyHelper, so the per-request refresh that "
        "carries the deployment pin would not happen",
    )
    toml = home / ".codex" / "config.toml"
    require(toml.is_file(), "codex setup wrote no config.toml")
    require(
        "adp-gateway" in toml.read_text(),
        "The Codex config does not name the ADP provider; requests would bypass ADP",
    )


def _run_tool(config, env, session, tool, marker, transcript):
    """One real model call through one tool, pinned to one deployment.

    Launched through the product's own launcher with `--deployment`, because the
    launcher is what pins the endpoint and the credential together; driving the
    tool directly would test the tool rather than the CLI.
    """
    name = session["name"]
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, marker))
    prompt = config.get("session_prompt", MARKER_PROMPT.format(marker=marker))
    tool_env = {**env, "ANTHROPIC_CUSTOM_HEADERS": f"X-Request-ID: {request_id}"}
    if tool == "claude":
        argv = [
            str(config["cli_path"]),
            "--deployment",
            name,
            "claude",
            "--print",
            "--model",
            config["claude_model"],
            prompt,
        ]
    else:
        argv = [
            str(config["cli_path"]),
            "--deployment",
            name,
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-c",
            'model_providers.adp-gateway.http_headers={"X-Request-ID"="'
            + request_id
            + '"}',
            prompt,
        ]
    if config.get("session_cwd"):
        # Both tools may run only the fixture's local barrier command. There are
        # no cloud credentials or repository checkout in this working directory.
        if tool == "claude":
            argv[4:4] = ["--allowedTools", "Bash", "--max-turns", "3"]
        else:
            argv[5:5] = ["--sandbox", "workspace-write"]
    transcript.append(common.sanitize(argv))
    began = time.monotonic()
    code, out, _err = common.bounded(
        argv,
        env=tool_env,
        timeout=int(config.get("inference_timeout_seconds", 300)),
        cwd=config.get("session_cwd"),
    )
    return {
        "deployment": name,
        "tool": tool,
        "marker": marker,
        "request_id": request_id,
        "started": began,
        "finished": time.monotonic(),
        "exit_code": code,
        "authentication_failed": bool(
            re.search(
                r"proxy_token_error|401|authentication.required|not signed in",
                (out or "") + (_err or ""),
                re.I,
            )
        ),
        "returned_marker": marker in (out or ""),
    }


def _proxy_identities(home, identifiers):
    """Which proxy belongs to which deployment, read from what each published.

    AC-04 in live form. Three concurrent Codex sessions cannot share the fixed
    9191 port, so each named deployment's proxy binds an OS-assigned one and
    publishes its identity to `<deployment>/runtime/proxy.json` after binding.
    Distinct ports AND correct attribution together are what show no session
    attached to another's proxy — distinct ports alone would not, since two
    proxies could still have crossed which deployment they serve.

    Keyed by the stable id, which is the directory name, so a record naming
    another deployment is detectable rather than assumed correct.
    """
    found = {}
    for name, identifier in identifiers.items():
        path = home / ".adp" / "deployments" / identifier / "runtime" / "proxy.json"
        try:
            identity = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        found[name] = {
            "port": identity.get("port"),
            "claims_deployment": identity.get("deployment"),
            "attributed_correctly": (
                str(identity.get("deployment_id") or "") == identifier
            ),
        }
    return found


def _usage_marker(config, session, token, marker, *, after, expect):
    """Did THIS deployment record a request carrying this marker?

    Read with that deployment's own token, from that deployment's own gateway and
    scoped to that deployment's own org, so an absence is the deployment's own
    account of what it did not receive.

    `expect` is what makes the wait bound honest in both directions. Expecting a
    record polls until it appears, because usage is written asynchronously.
    Expecting an ABSENCE cannot poll-until-true — it would return on the first
    empty read, before a crossed request had had time to be recorded — so it waits
    a fixed window and then reads once. An absence proven in less time than a
    presence takes to appear is not an absence.
    """

    def look():
        _status, payload = common.api(
            {**config, "gateway_url": session["gateway_url"]},
            "/usage/logs?"
            + urlencode(
                {
                    "org_id": session["org_id"],
                    "user_id": session["user_id"],
                    "start_date": after,
                    "limit": 100,
                }
            ),
            token,
        )
        require(
            not (payload or {}).get("has_more"),
            "Usage window exceeds one page; absence cannot be established",
        )
        for row in (payload or {}).get("items") or []:
            if str(row.get("timestamp") or "") < after:
                continue
            if row.get("request_id") == marker:
                return row
        return None

    window = int(config.get("usage_wait_seconds", 180))
    if expect:
        return common.wait_for(look, timeout=window, interval=10)
    time.sleep(min(window, int(config.get("absence_wait_seconds", 60))))
    return look()


def _receipts(config, sessions, tokens, runs, *, after):
    """The ledger assertion, both halves, for every marker in a pass.

    The second half is the one a weaker journey omits. It is not enough that each
    marker reached its own deployment, because that is equally true of a CLI that
    broadcast every request to all three — so each marker must also be ABSENT from
    the other two deployments' own usage logs.
    """
    receipts, leaks, unrecorded = [], [], []
    for run in runs:
        owner = run["deployment"]
        record = _usage_marker(
            config,
            sessions[owner],
            tokens[owner],
            run["request_id"],
            after=after,
            expect=True,
        )
        if record is None:
            unrecorded.append(f"{owner}/{run['tool']}")
            continue
        require(
            record.get("status_code") == 200
            and int(record.get("output_tokens") or 0) > 0,
            f"{owner}/{run['tool']} has no successful metered completion",
        )
        attributed = str(record.get("user_id") or "")
        require(
            attributed == sessions[owner]["user_id"],
            f"{owner} attributed its own {run['tool']} request to {attributed!r}, "
            "not to the identity that signed in there",
        )
        receipts.append(
            {
                "deployment": owner,
                "tool": run["tool"],
                "status_code": record.get("status_code"),
                "model": record.get("model"),
                "request_id": record.get("request_id"),
                "output_tokens": record.get("output_tokens"),
            }
        )
    require(
        not unrecorded,
        "No usage carrying the marker was recorded at its own deployment for: "
        + ", ".join(unrecorded)
        + ". The request cannot be attributed to the deployment it was aimed at",
    )
    # One observation window covers every pair; do not sleep once per pair.
    time.sleep(
        min(
            int(config.get("usage_wait_seconds", 180)),
            int(config.get("absence_wait_seconds", 60)),
        )
    )
    for run in runs:
        owner = run["deployment"]
        for other in sessions:
            if other == owner:
                continue
            crossed = _usage_marker(
                {**config, "absence_wait_seconds": 0},
                sessions[other],
                tokens[other],
                run["request_id"],
                after=after,
                expect=False,
            )
            if crossed is not None:
                leaks.append({"aimed_at": owner, "also_reached": other})
    require(
        not leaks,
        "A request aimed at one deployment was also recorded by another: "
        + json.dumps(leaks),
    )
    return receipts


def _pass(config, env, sessions, tokens, evidence, *, label):
    """One arrangement of tools across the three deployments, run CONCURRENTLY.

    Concurrency is the requirement, not an optimization: three sequential calls
    would pass on a CLI that could only hold one deployment at a time, which is
    the defect this story exists to remove. So all three are started before any is
    waited on.
    """
    evidence["stage"] = label
    names = list(sessions)
    tools = ARRANGEMENTS[label]
    # A minute of slack before the first launch, because usage timestamps come
    # from the gateway's clock and not this instance's.
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))

    runs, failures = [], []
    lock = threading.Lock()

    def drive(name, tool):
        marker = f"{config['evaluation_id']}-{label}-{name}-{tool}"
        try:
            result = _run_tool(
                config, env, sessions[name], tool, marker, evidence["transcript"]
            )
        except Exception as exc:
            with lock:
                failures.append(f"{name}/{tool}: {exc}")
            return
        with lock:
            runs.append(result)

    threads = [
        threading.Thread(
            target=drive, args=(name, tool), name=f"{label}:{name}/{tool}", daemon=True
        )
        for name, tool in zip(names, tools, strict=True)
    ]
    for thread in threads:
        thread.start()
    # Bounded join. `_run_tool` already bounds each child, so this only has to
    # outlast that bound; an unbounded join on an instance that bills by the
    # second is a cost bug as much as a hang.
    deadline = int(config.get("inference_timeout_seconds", 300)) + 120
    for thread in threads:
        thread.join(timeout=deadline)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    require(not stuck, f"{label}: a tool session never returned: {', '.join(stuck)}")
    require(not failures, f"{label}: " + "; ".join(failures))
    require(
        len(runs) == 3 and {run["deployment"] for run in runs} == set(names),
        f"{label}: not all three tool sessions produced a result",
    )
    require(
        max(run["started"] for run in runs) < min(run["finished"] for run in runs),
        f"{label}: the three tool sessions did not overlap",
    )

    refused = [run for run in runs if run["exit_code"] != 0]
    require(not refused, f"{label}: a tool session exited non-zero: {refused}")
    silent = [
        f"{run['deployment']}/{run['tool']}"
        for run in runs
        if not run["returned_marker"]
    ]
    require(
        not silent,
        f"{label}: no completion of our prompt came back from: " + ", ".join(silent),
    )
    return {
        "arrangement": dict(zip(names, tools, strict=True)),
        "concurrent_sessions": len(runs),
        "receipts": _receipts(config, sessions, tokens, runs, after=started),
    }


def _overlap(config, env, home, sessions, tokens, identifiers, evidence):
    """E16: the concurrent-overlap proof, in both tool arrangements."""
    evidence["overlap"] = _pass(
        config, env, sessions, tokens, evidence, label="overlap"
    )

    evidence["reverse"] = _pass(
        config, env, sessions, tokens, evidence, label="reverse"
    )

    identities = _proxy_identities(home, identifiers)
    require(
        set(identities) == set(identifiers),
        "Not every deployment published a proxy identity, so the Codex sessions cannot be "
        "shown to have used per-deployment proxies",
    )
    crossed = [
        name for name, entry in identities.items() if not entry["attributed_correctly"]
    ]
    require(
        not crossed,
        "A proxy in one deployment's runtime directory claims a different "
        f"deployment: {crossed}",
    )
    ports = [entry["port"] for entry in identities.values() if entry.get("port")]
    require(
        len(set(ports)) == len(ports),
        f"Two deployments' proxies published the same port: {identities}",
    )
    evidence["proxies"] = identities
    evidence["checks"].append("each_deployment_proxy_holds_its_own_attributed_port")

    evidence["checks"].append("both_tool_arrangements_overlap_without_crossing")
    evidence["correlation"] = {
        "overlap_request_ids": [
            receipt["request_id"] for receipt in evidence["overlap"]["receipts"]
        ],
        "reverse_request_ids": [
            receipt["request_id"] for receipt in evidence["reverse"]["receipts"]
        ],
    }
    evidence["detail"] = {
        "overlap": evidence["overlap"],
        "reverse": evidence["reverse"],
        "proxies": identities,
        "checks": evidence["checks"],
    }


def _lifecycle_changes(config, cli, sessions, evidence):
    """E17: a default switch, a refresh, and one logout — live.

    Ordered so each step's subject is still intact when it runs: the switch and the
    refresh both need three live sessions, and the logout destroys one, so it is
    last. A logout first would make the two checks after it untestable.
    """
    names = list(sessions)
    switch_to, refreshed, logged_out = names[2], names[1], names[0]

    # 1. The saved default moves. A command with no selection must follow the new
    # default, and no other deployment's session may be touched — a pin that
    # outlived the switch would be indistinguishable from a switch that silently
    # did nothing.
    evidence["stage"] = "default_switch"
    cli.json(["deployment", "use", switch_to])
    listed = cli.json(["deployment", "list"])
    require(
        str(listed.get("default") or "") == switch_to,
        f"After `deployment use {switch_to}` the CLI still reports "
        f"{listed.get('default')!r} as the saved default",
    )
    require(
        str(listed.get("effective") or "") == switch_to,
        "A command with no selection did not follow the new default",
    )
    signed_in = {
        str(row.get("name")): bool(row.get("signed_in"))
        for row in listed.get("deployments") or []
    }
    lost = [name for name in names if not signed_in.get(name)]
    require(
        not lost,
        "Changing the saved default invalidated the session of: " + ", ".join(lost),
    )
    evidence["default_switch"] = {"now": switch_to, "all_three_still_signed_in": True}
    evidence["checks"].append("default_switch_leaves_other_sessions_signed_in")

    # 2. A refresh of one deployment. The token must change for that deployment and
    # for no other: a shared refresh lock or a shared token store would rotate all
    # three, which is the AC-03 failure.
    evidence["stage"] = "refresh"
    before = {name: _access_token(cli, name) for name in names}
    require(
        len(set(before.values())) == len(names),
        "Two deployments hand back the same access token, so they share a session "
        "store and no later assertion could tell them apart",
    )
    code, _payload = cli.run(
        ["--deployment", refreshed, "refresh"], expected=None, json_output=False
    )
    require(code == 0, f"`adp --deployment {refreshed} refresh` failed")
    after = {name: _access_token(cli, name) for name in names}
    require(
        after[refreshed] != before[refreshed],
        "The explicit refresh did not rotate the selected deployment's access token",
    )
    rotated = [
        name for name in names if name != refreshed and after[name] != before[name]
    ]
    require(
        not rotated,
        "Refreshing one deployment rotated another's token: " + ", ".join(rotated),
    )
    # Only WHETHER each token changed is published; no token leaves the instance.
    evidence["refresh"] = {
        "refreshed": refreshed,
        "others_rotated": rotated,
        "still_distinct_per_deployment": len(set(after.values())) == len(names),
    }
    evidence["checks"].append("refresh_is_per_deployment")

    # 3. Logging out of one. The other two must keep working, and the logged-out
    # one must fail SAYING so rather than borrowing a session that still exists.
    evidence["stage"] = "logout"
    cli.run(["--deployment", logged_out, "logout"], expected=0, json_output=False)
    listed = cli.json(["deployment", "list"])
    signed_in = {
        str(row.get("name")): bool(row.get("signed_in"))
        for row in listed.get("deployments") or []
    }
    require(
        not signed_in.get(logged_out),
        f"{logged_out} still reports a session after being logged out",
    )
    collateral = [
        name for name in names if name != logged_out and not signed_in.get(name)
    ]
    require(
        not collateral,
        f"Logging out of {logged_out} also ended the session of: "
        + ", ".join(collateral),
    )
    code, payload = cli.run(["--deployment", logged_out, "aws", "list"], expected=None)
    require(
        code != 0,
        f"A command against the logged-out {logged_out} succeeded; it borrowed "
        "another deployment's session",
    )
    error = ((payload or {}).get("error") or {}).get("code") or ""
    require(
        error == "authentication_required",
        f"The logged-out failure was labelled {error!r} rather than as an "
        "authentication problem the user can act on",
    )
    # The other two must still WORK, not merely still claim a session: `signed_in`
    # is a file check, and a token that no longer resolves would pass it.
    for name in names:
        if name == logged_out:
            continue
        _access_token(cli, name)
    evidence["logout"] = {
        "logged_out": logged_out,
        "others_still_usable": True,
        "failure_code": error,
    }
    evidence["checks"].append("logout_is_per_deployment_and_labelled")
    evidence["detail"] = {
        "default_switch": evidence["default_switch"],
        "refresh": evidence["refresh"],
        "logout": evidence["logout"],
        "checks": evidence["checks"],
    }


def _lifecycle(config, cli, env, home, sessions, evidence):
    """Continue real model sessions after changing shared deployment state.

    Each model must first run a local command that waits at a barrier. Only once
    all three commands are waiting do we switch, refresh and log out. Releasing
    the barriers then requires a further model response in the SAME processes.
    The reply token exists only in the released file, so echoing the prompt
    without actually reaching the barrier cannot pass this test.
    """
    workspace = home / "lifecycle"
    workspace.mkdir(mode=0o700)
    (workspace / "gate.py").write_text(
        "import pathlib, sys, time\n"
        "name = sys.argv[1]\n"
        "pathlib.Path(name + '.ready').touch()\n"
        "release = pathlib.Path(name + '.release')\n"
        "deadline = time.monotonic() + 180\n"
        "while not release.exists():\n"
        "    if time.monotonic() > deadline: raise SystemExit('barrier timed out')\n"
        "    time.sleep(0.1)\n"
        "print(release.read_text())\n"
    )
    names = list(sessions)
    results, errors = {}, []
    lock = threading.Lock()
    markers = {name: f"{config['evaluation_id']}-lifecycle-{name}" for name in names}

    def drive(name, tool):
        try:
            prompt = (
                "Run the following command once using your shell tool, with a 180 second timeout, "
                "and wait for it to finish. Then reply with exactly the token it prints, nothing else: "
                f"python3 gate.py {shlex.quote(name)}"
            )
            run = _run_tool(
                {**config, "session_prompt": prompt, "session_cwd": str(workspace)},
                env,
                sessions[name],
                tool,
                markers[name],
                evidence["transcript"],
            )
            with lock:
                results[name] = run
        except Exception as exc:
            with lock:
                errors.append(f"{name}/{tool}: {exc}")

    threads = [
        threading.Thread(target=drive, args=(name, tool), daemon=True)
        for name, tool in zip(names, ("codex", "codex", "claude"), strict=True)
    ]
    audit_tokens = {name: _access_token(cli, name) for name in names}
    for thread in threads:
        thread.start()
    try:
        ready = common.wait_for(
            lambda: all((workspace / f"{name}.ready").exists() for name in names),
            timeout=min(int(config.get("inference_timeout_seconds", 300)), 150),
            interval=1,
        )
        require(
            ready and all(thread.is_alive() for thread in threads),
            "All three model sessions must reach the local barrier before lifecycle changes",
        )
        _lifecycle_changes(config, cli, sessions, evidence)
        continued_after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    finally:
        for name in names:
            (workspace / f"{name}.release").write_text(markers[name])
        deadline = (
            time.monotonic() + int(config.get("inference_timeout_seconds", 300)) + 15
        )
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        require(
            not any(thread.is_alive() for thread in threads),
            "A lifecycle tool process did not stop",
        )
    require(
        not errors and set(results) == set(names),
        "Lifecycle sessions failed: " + "; ".join(errors),
    )
    # The logged-out session uses Codex, whose proxy fetches auth on each call.
    # Its continuation must fail, while both other sessions complete the token
    # returned by their barrier command after the state changes.
    require(
        results[names[0]]["exit_code"] not in (0, 124)
        and results[names[0]]["authentication_failed"]
        and not results[names[0]]["returned_marker"],
        "The logged-out model session continued using credentials",
    )
    for name in names[1:]:
        require(
            results[name]["exit_code"] == 0 and results[name]["returned_marker"],
            f"The existing model session for {name} did not continue after lifecycle changes",
        )
        audit_tokens[name] = _access_token(cli, name)
    receipts = _receipts(
        config,
        sessions,
        audit_tokens,
        [results[name] for name in names[1:]],
        after=continued_after,
    )
    evidence["checks"].append(
        "existing_model_sessions_continue_after_lifecycle_changes"
    )
    evidence["detail"]["model_sessions"] = {
        name: {
            "tool": run["tool"],
            "exit_code": run["exit_code"],
            "returned_marker": run["returned_marker"],
        }
        for name, run in results.items()
    }
    evidence["detail"]["continued_usage"] = receipts


def _teardown(cli, home, identifiers, evidence):
    """Stop what we started and forget the local records.

    The proxies matter most: three listeners left behind would hold their ports
    against a resumed attempt on this same instance, and unlike the
    single-deployment journeys there is one per deployment to account for.
    `common.stop_proxy` expects a HOME and derives `.bedrock-gateway/proxy.pid`
    from it — the LEGACY layout — so each named deployment's own runtime directory
    is passed instead.

    `adp deployment remove` refuses the saved default by design, because removing
    it would leave the next command with no target. So the default is pointed at
    one record, the other two are removed, and the last is REPORTED as retained
    rather than forced — a teardown that fought a deliberate product rule would be
    testing the harness's opinion instead of the product's behaviour.
    """
    evidence["stage"] = "teardown"
    stopped = {
        name: common.stop_proxy_runtime(
            home / ".adp" / "deployments" / identifier / "runtime"
        )
        for name, identifier in identifiers.items()
    }

    names = list(identifiers)
    if not names:
        return {}
    keeper = names[0]
    cli.run(["deployment", "use", keeper], expected=None)
    removed = []
    for name in names[1:]:
        # `expected=None`: a record already gone is the desired end state, and a
        # teardown that failed the case for succeeding twice would be a harness
        # defect rather than a product one.
        code, _payload = cli.run(["deployment", "remove", name], expected=None)
        if code == 0:
            removed.append(name)
    listed = cli.run(["deployment", "list"], expected=None)[1] or {}
    evidence["teardown"] = {
        "proxies_stopped": stopped,
        "removed": removed,
        "retained_default": keeper,
        "records_remaining": [
            str(row.get("name")) for row in listed.get("deployments") or []
        ],
    }
    require(all(stopped.values()), "Failed to stop every deployment proxy")
    require(
        set(evidence["teardown"]["records_remaining"]) == {keeper},
        "Cleanup left unexpected deployment records",
    )
    return evidence["teardown"]


def _validate_bindings(config):
    """Refuse a binding set that could not prove isolation whatever it observed.

    Checked again here, on the instance, even though `config.validate()` has
    already refused the same shapes offline: this module is also the thing a
    hand-written payload or a resumed run reaches, and a weak fixture that got past
    the orchestrator would otherwise produce a confident green.
    """
    deployments = config["deployments"]
    require(
        len(deployments) == 3,
        f"This journey needs three deployments; {len(deployments)} were supplied. "
        "Two cannot distinguish 'each command reached its own' from 'commands "
        "alternated between the two'",
    )
    for entry in deployments:
        for key in ("name", "gateway_url", "credential_secret_name"):
            require(
                str(entry.get(key) or ""),
                f"A deployment binding is missing {key!r}; it cannot be signed in to",
            )
    require(
        len({str(entry["gateway_url"]).rstrip("/") for entry in deployments})
        == len(deployments),
        "Two deployment bindings share a gateway URL. The CLI treats those as "
        "aliases of one deployment — one stable id, one session — so this run could "
        "not prove isolation no matter what it observed",
    )
    require(
        len({str(entry["credential_secret_name"]) for entry in deployments})
        == len(deployments),
        "Two deployment bindings share a credential reference, so the three logins "
        "would be the same identity and a per-deployment logout could not be shown",
    )


def execute(config, evidence):
    os.umask(0o077)
    missing = [key for key in REQUIRED if not config.get(key)]
    require(
        not missing,
        "The multi-deployment journey was invoked without: " + ", ".join(missing),
    )
    require(
        config["mode"] in ("overlap", "lifecycle"),
        f"Unknown multi-deployment mode {config['mode']!r}",
    )
    _validate_bindings(config)
    deployments = config["deployments"]

    with tempfile.TemporaryDirectory(prefix="adp-multi-deployment-") as temporary:
        home, env = _home(config, temporary)
        cli = common.Cli(Path(config["cli_path"]), env, evidence["transcript"])
        identifiers = {}
        try:
            _register(cli, deployments, evidence, identifiers)

            evidence["stage"] = "login"
            sessions, tokens = {}, {}
            for entry in deployments:
                sessions[entry["name"]] = _login(config, cli, env, entry, evidence)
                tokens[entry["name"]] = _access_token(cli, entry["name"])
            require(
                len(set(tokens.values())) == len(tokens),
                "Three separate logins produced a shared token, so the sessions are "
                "not independent and no later assertion could distinguish them",
            )
            evidence["sessions"] = {
                name: {"signed_in": True, "org_id": session["org_id"]}
                for name, session in sessions.items()
            }
            evidence["checks"].append("three_independent_sessions_in_one_home")

            for entry in deployments:
                _setup_tools(cli, entry["name"], home)
            evidence["checks"].append("both_tools_set_up_for_all_three_deployments")

            if config["mode"] == "overlap":
                _overlap(config, env, home, sessions, tokens, identifiers, evidence)
            else:
                _lifecycle(config, cli, env, home, sessions, evidence)
        finally:
            # In `finally` because a failed assertion must not leave three proxies
            # listening: the next attempt on this instance would find them and fail
            # for a reason that has nothing to do with the product.
            if identifiers:
                try:
                    _teardown(cli, home, identifiers, evidence)
                except Exception as exc:
                    evidence["teardown_error"] = str(exc)
                    evidence["success"] = False
                    raise
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
