#!/usr/bin/env python3
"""Build a SkyPilot resource request; SkyPilot alone chooses the machine.

Standard library only. Writes public task JSON to stdout. Does not discover
credentials, query pricing, launch machines or configure an EKS cluster.
"""

import argparse
import json
import re


class InvalidRequest(ValueError):
    pass


def bounded(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidRequest(f"{label} must be between {minimum} and {maximum}")
    return value


def capacity_task(
    *,
    name,
    gpus,
    nodes,
    disk_gb,
    hold_seconds,
    clouds=(),
    region=None,
    cpus=None,
    memory_gb=None,
    spot=False,
):
    """Return one task with a set of eligible placements, never a ranked offer.

    Cloud alternatives describe one allocation. Call separately for each cloud
    when the user needs simultaneous capacity from more than one provider.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise InvalidRequest("name must be a lowercase SkyPilot cluster name")
    bounded(nodes, 1, 16, "nodes")
    bounded(disk_gb, 20, 2048, "disk GB")
    bounded(hold_seconds, 1, 86400, "hold seconds")
    if type(spot) is not bool:
        raise InvalidRequest("spot must be a boolean")
    if not isinstance(gpus, (tuple, list)) or not 1 <= len(gpus) <= 8:
        raise InvalidRequest("supply between one and eight acceptable GPU choices")
    accelerators = []
    for gpu in gpus:
        if not isinstance(gpu, str):
            raise InvalidRequest("GPU choices must use TYPE:COUNT")
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]{0,63}):([1-8])", gpu)
        if match is None:
            raise InvalidRequest("GPU choices must use TYPE:COUNT with count 1-8")
        choice = {match[1]: int(match[2])}
        if choice in accelerators:
            raise InvalidRequest("duplicate GPU choice")
        accelerators.append(choice)
    if (
        not isinstance(clouds, (tuple, list))
        or len(clouds) > 3
        or any(
            not isinstance(c, str) or c not in {"aws", "nebius", "lambda"}
            for c in clouds
        )
        or len(set(clouds)) != len(clouds)
    ):
        raise InvalidRequest("clouds must be distinct choices from aws, nebius, lambda")
    if region is not None and (
        len(clouds) != 1
        or not isinstance(region, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", region)
    ):
        raise InvalidRequest("a region requires exactly one selected cloud")
    resources = {"disk_size": disk_gb, "use_spot": spot}
    if cpus is not None:
        resources["cpus"] = f"{bounded(cpus, 1, 1024, 'CPUs')}+"
    if memory_gb is not None:
        resources["memory"] = f"{bounded(memory_gb, 1, 16384, 'memory GB')}+"
    options = []
    for cloud in clouds or (None,):
        for accelerator in accelerators:
            option = {"accelerators": accelerator}
            if cloud is not None:
                # These fields are accepted by the pinned 0.12.0 task schema.
                option["cloud"] = cloud
            if region is not None:
                option["region"] = region
            options.append(option)
    if len(options) == 1:
        resources.update(options[0])
    else:
        resources["any_of"] = options
    return {
        "name": name,
        "num_nodes": nodes,
        "resources": resources,
        "run": f"sleep {hold_seconds}",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--gpu", dest="gpus", action="append", required=True)
    parser.add_argument("--nodes", type=int, required=True)
    parser.add_argument("--disk-gb", type=int, required=True)
    parser.add_argument("--hold-seconds", type=int, required=True)
    parser.add_argument("--cloud", dest="clouds", action="append", default=[])
    parser.add_argument("--region")
    parser.add_argument("--cpus", type=int)
    parser.add_argument("--memory-gb", type=int)
    parser.add_argument("--spot", action="store_true")
    args = parser.parse_args(argv)
    try:
        task = capacity_task(**vars(args))
    except InvalidRequest as exc:
        parser.error(str(exc))
    print(json.dumps(task, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
