"""Verify an installed Insights CLI against the independently read serving API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, destination):
        raise ValueError("Insights readback redirected outside the selected release")


def protected_input(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Insights fixture must be an absolute private file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise ValueError("Insights fixture is not private and bounded")
        return json.load(source)


def read_results(url, token):
    opener = build_opener(RefuseRedirect())
    try:
        with opener.open(Request(url, headers={"Authorization": "Bearer " + token}), timeout=30) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        with error:
            return error.code, json.load(error)


def verify_cli_checkpoint(fixture, release, binary, *, run=subprocess.run, read=read_results):
    required = ("run_id", "source_revision", "project_id", "product_id", "as_of",
                "portfolio_ids", "gateway_url", "authorized_home", "revoked_home",
                "authorized_token", "revoked_token")
    if not isinstance(fixture, dict) or not all(fixture.get(name) for name in required):
        raise ValueError("V13 requires a protected run-owned two-actor fixture")
    if (not isinstance(release, dict) or release.get("source_revision") != fixture["source_revision"]
            or not re.fullmatch(r"product-[1-9][0-9]*-[1-9][0-9]*", fixture["run_id"])
            or not isinstance(release.get("read_service_receipt"), str)
            or not release["read_service_receipt"]
            or not isinstance(release.get("installed_cli_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", release["installed_cli_sha256"])
            or hashlib.sha256(Path(binary).read_bytes()).hexdigest() != release["installed_cli_sha256"]):
        raise ValueError("V13 CLI or read service differs from the pinned release")
    if not isinstance(fixture["portfolio_ids"], list):
        raise ValueError("V13 needs an owned Portfolio fixture")
    for identifier in (fixture["project_id"], fixture["product_id"], *fixture["portfolio_ids"]):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", identifier):
            raise ValueError("V13 fixture has an invalid project, product or Portfolio")
    gateway = fixture["gateway_url"]
    target = urlsplit(gateway)
    if (not isinstance(gateway, str) or target.scheme != "https" or not target.hostname or target.username or target.password
            or target.query or target.fragment or not gateway.endswith("/api")):
        raise ValueError("V13 requires the selected HTTPS gateway API")
    if (not isinstance(fixture["portfolio_ids"], list) or len(fixture["portfolio_ids"]) != 2
            or len(set(fixture["portfolio_ids"])) != 2):
        raise ValueError("V13 needs two overlapping Portfolios")
    for home in (fixture["authorized_home"], fixture["revoked_home"]):
        configured = json.loads((Path(home) / ".bedrock-gateway/config.json").read_text())
        if configured.get("gateway_url") != gateway:
            raise ValueError("V13 installed CLI points at a different release")
    path = (gateway + "/engineering-insights/v1/projects/" + fixture["project_id"]
            + "/results?" + urlencode([("view", "business_context"),
                                     ("product_id", fixture["product_id"]), ("as_of", fixture["as_of"]),
                                     *(("portfolio_id", item) for item in fixture["portfolio_ids"]) ]))
    command = [str(binary), "insights", "context", "show", "--project", fixture["project_id"],
               "--product", fixture["product_id"], "--as-of", fixture["as_of"]]
    for portfolio in fixture["portfolio_ids"]:
        command += ["--portfolio", portfolio]
    command.append("--json")

    def invoke(home):
        result = run(command, env={**os.environ, "HOME": home}, capture_output=True, text=True, timeout=120)
        try:
            output = json.loads(result.stdout)
        except (TypeError, ValueError) as error:
            raise ValueError("V13 installed CLI did not return JSON") from error
        return result.returncode, output

    exit_code, cli = invoke(fixture["authorized_home"])
    status, api = read(path, fixture["authorized_token"])
    if (exit_code != 0 or status != 200 or cli.get("status") != "ok"
            or cli.get("detail") != api or api.get("coverage") != "complete"
            or api.get("cost_by_currency") != {"USD": "10.00"} or api.get("work_count") != 1
            or api.get("project_id") != fixture["project_id"]
            or api.get("product_id") != fixture["product_id"]):
        raise AssertionError("V13 installed CLI and independent readback do not agree")
    exit_code, cli = invoke(fixture["revoked_home"])
    status, api = read(path, fixture["revoked_token"])
    if (exit_code != 5 or cli.get("status") != "failed"
            or cli.get("error", {}).get("code") != "not_found" or cli.get("detail") != {}
            or status != 404 or api.get("error", {}).get("code") != "not_found"):
        raise AssertionError("V13 revoked actor disclosed a result or read access remained")
    return True
