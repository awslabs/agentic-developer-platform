#!/usr/bin/env python3
"""Read the current render step's SSM parameters in batches, without logging values."""

import argparse
import json
import os
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class BatchReadDeniedError(RuntimeError):
    pass


def aws(arguments):
    result = subprocess.run(["aws", "ssm", *arguments, "--output", "json"], capture_output=True, text=True, timeout=120)
    if result.returncode:
        if arguments[0] == "get-parameters" and "AccessDeniedException" in result.stderr:
            raise BatchReadDeniedError("Batch SSM reads are not permitted")
        if arguments[0] == "get-parameter" and "ParameterNotFound" in result.stderr:
            return {}
        # Include neither parameter values nor raw SDK errors in deployment logs.
        raise RuntimeError("SSM configuration read failed")
    return json.loads(result.stdout)


def read_parameters(source, environment, call=aws):
    groups = {"plain": set(), "decrypted": set()}
    for getter, raw in re.findall(r'(get_ssm|get_authority_ssm) "([^"]+)"', source):
        name = raw.replace("${ENVIRONMENT}", environment)
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", name):
            raise ValueError("Unsupported parameter name in render step")
        groups["decrypted" if getter == "get_authority_ssm" else "plain"].add(name)
    values = {}
    individual = False
    for mode, names in groups.items():
        names = sorted(names)
        decrypt = ["--with-decryption"] if mode == "decrypted" else []

        def read_one(name):
            response = call(["get-parameter", "--name", name, *decrypt])
            item = response.get("Parameter")
            if item is None:
                return name, None
            if item.get("Name") != name or not isinstance(item.get("Value"), str):
                raise ValueError("Invalid individual SSM configuration response")
            return name, item["Value"]

        for start in range(0, len(names), 10):
            batch = names[start : start + 10]
            if not individual:
                try:
                    response = call(["get-parameters", "--names", *batch, *decrypt])
                except BatchReadDeniedError:
                    # Existing deployment roles may grant GetParameter only.
                    # Preserve that scope and overlap the independent reads.
                    individual = True
            if individual:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for name, value in pool.map(read_one, batch):
                        if value is not None:
                            values[f"{mode}:{name}"] = value
                continue
            seen = set()
            for item in response.get("Parameters", []):
                name = item["Name"]
                if name not in batch or name in seen or not isinstance(item["Value"], str):
                    raise ValueError("Invalid SSM configuration response")
                seen.add(name)
                values[f"{mode}:{name}"] = item["Value"]
            missing = set(response.get("InvalidParameters", []))
            if seen & missing or seen | missing != set(batch):
                raise ValueError("Incomplete SSM configuration response")
    return values


def shell_values(values):
    entries = "\n".join(f"  [{shlex.quote(key)}]={shlex.quote(value)}" for key, value in sorted(values.items()))
    return f"declare -A ADP_SSM_VALUES=(\n{entries}\n)\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("environment")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    values = read_parameters(args.source.read_text(), args.environment)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
        output.write(shell_values(values))
