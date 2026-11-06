#!/usr/bin/env python3
"""Retain the target's Product dispatch choice without exposing runtime state."""

import argparse
import json
import os
import re
import subprocess
import sys


INPUT = "orchestration_product_story_dispatch_enabled"
ENVIRONMENT = "ADP_PRODUCT_STORY_DISPATCH_ENABLED"
MODULE = "module.orchestration_tick[0]"
ADDRESS = MODULE + ".aws_lambda_function.tick"


def runtime_setting(variables):
    value = variables.get(ENVIRONMENT, "false")
    if not isinstance(value, str) or value.lower() not in ("true", "false"):
        raise ValueError("Invalid Product dispatch runtime setting; expected true or false")
    return value.lower() == "true"


def owned_ticks(state):
    ticks = [instance["attributes"] for resource in state.get("resources", [])
             if resource.get("mode") == "managed" and resource.get("module") == MODULE
             and resource.get("type") == "aws_lambda_function" and resource.get("name") == "tick"
             for instance in resource.get("instances", [])]
    if len(ticks) > 1:
        raise ValueError("Ambiguous retained Product dispatch engine")
    return ticks


def product_dispatch_settings(state):
    saved = state.get("outputs", {}).get("release_configuration", {}).get("value", {})
    if not isinstance(saved, dict) or (INPUT in saved and type(saved[INPUT]) is not bool):
        raise ValueError("Invalid retained Product dispatch configuration")
    ticks = owned_ticks(state)
    if ticks:
        environment = ticks[0].get("environment")
        if not isinstance(environment, list) or len(environment) != 1 or not isinstance(environment[0].get("variables"), dict):
            raise ValueError("Cannot recover retained Product dispatch engine environment")
        # A targeted apply can leave the root output stale. Prefer the exact
        # owned resource, then refresh from live AWS in the deployment preflight.
        return {INPUT: runtime_setting(environment[0]["variables"])}
    return {INPUT: saved[INPUT]} if INPUT in saved else {}


def from_show(document):
    if not isinstance(document, dict) or document.get("format_version") != "1.0":
        raise ValueError("Invalid Product dispatch owner state")
    if "values" not in document:
        if set(document) != {"format_version"}:
            raise ValueError("Invalid Product dispatch owner state")
        return {}
    values = document["values"]
    resources = []

    def visit(module):
        for resource in module.get("resources", []):
            if resource.get("address") == ADDRESS and resource.get("mode") == "managed":
                resources.append({"mode": "managed", "module": MODULE, "type": "aws_lambda_function", "name": "tick",
                                  "instances": [{"attributes": resource["values"]}]})
        for child in module.get("child_modules", []):
            visit(child)

    visit(values.get("root_module", {}))
    return {"outputs": values.get("outputs", {}), "resources": resources}


def live_setting(*, account, region, environment, tracked):
    def aws(*args):
        return json.loads(subprocess.check_output(["aws", *args, "--region", region, "--output", "json"],
                                                 text=True, stderr=subprocess.PIPE))

    if aws("sts", "get-caller-identity")["Account"] != account:
        raise ValueError("Product dispatch target differs from AWS identity")
    function = f"arn:aws:lambda:{region}:{account}:function:adp-{environment}-orchestration-tick"
    try:
        config = aws("lambda", "get-function-configuration", "--function-name", function)
    except subprocess.CalledProcessError as error:
        if not tracked and re.search(r"\(ResourceNotFoundException\)", error.stderr or ""):
            return None
        raise
    if not tracked:
        raise ValueError("Product dispatch engine is outside gateway state; review and import before applying")
    if config.get("FunctionArn") != function or config.get("State") != "Active" or config.get("LastUpdateStatus") != "Successful":
        raise ValueError("Product dispatch engine identity or readiness could not be verified")
    return runtime_setting(config.get("Environment", {}).get("Variables", {}))


def effective_settings(state, live, requested=""):
    retained = product_dispatch_settings(state)
    if requested:
        if requested not in ("true", "false"):
            raise ValueError("Protected Product dispatch setting must be true or false")
        return {INPUT: requested == "true"}
    if live is not None:
        if type(live) is not bool:
            raise ValueError("Invalid live Product dispatch setting")
        return {INPUT: live}
    return {INPUT: retained.get(INPUT, False)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "region", "environment"):
        parser.add_argument("--" + name, required=True)
    args = vars(parser.parse_args())
    try:
        state = from_show(json.load(sys.stdin))
        live = live_setting(**args, tracked=bool(owned_ticks(state)))
        print(json.dumps(effective_settings(state, live, os.environ.get(ENVIRONMENT, ""))))
    except (ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError):
        raise SystemExit("Cannot retain Product dispatch input; verify the target engine and protected ADP_PRODUCT_STORY_DISPATCH_ENABLED setting") from None


if __name__ == "__main__":
    main()
